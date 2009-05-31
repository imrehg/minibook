#!/usr/bin/env python
""" Minibook: the Facebook(TM) status updater
(C) 2009 Gergely Imreh <imrehg@gmail.com>
"""

VERSION = '0.1.0'
APPNAME = 'minibook'
MIT = """
Copyright (c) 2009 Gergely Imreh <imrehg@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
MAX_MESSAGE_LENGTH = 255

import pygtk
pygtk.require('2.0')
import gtk
import gobject
try:
    from facebook import Facebook
except:
    print "Pyfacebook is not available, cannot run."
    exit(1)

import time
import re
import threading

gobject.threads_init()
gtk.gdk.threads_init()
gtk.gdk.threads_enter()

try:
    import gtkspell
    spelling_support = True
except:
    spelling_support = False

import logging
import sys
import timesince
import urllib2

LEVELS = {'debug': logging.DEBUG,
          'info': logging.INFO,
          'warning': logging.WARNING,
          'error': logging.ERROR,
          'critical': logging.CRITICAL}

if len(sys.argv) > 1:
    level_name = sys.argv[1]
    level = LEVELS.get(level_name, logging.CRITICAL)
    logging.basicConfig(level=level)
else:
    logging.basicConfig(level=logging.CRITICAL)

_log = logging.getLogger('minibook')


class Columns:
    (STATUSID, UID, STATUS, DATETIME, COMMENTS, LIKES) = range(6)


#-------------------------------------------------
# Threading related objects.
# Info http://edsiper.linuxchile.cl/blog/?p=152
# to mitigate TreeView + threads problems
# These classes are based on the code available at http://gist.github.com/51686
# (c) 2008, John Stowers <john.stowers@gmail.com>
#-------------------------------------------------

class _IdleObject(gobject.GObject):
    """
    Override gobject.GObject to always emit signals in the main thread
    by emmitting on an idle handler
    """

    def __init__(self):
        gobject.GObject.__init__(self)

    def emit(self, *args):
        gobject.idle_add(gobject.GObject.emit, self, *args)


#-------------------------------------------------
# Thread support
#-------------------------------------------------

class _WorkerThread(threading.Thread, _IdleObject):
    """
    A single working thread.
    """

    __gsignals__ = {
            "completed": (
                gobject.SIGNAL_RUN_LAST,
                gobject.TYPE_NONE,
                (gobject.TYPE_PYOBJECT, )),
            "exception": (
                gobject.SIGNAL_RUN_LAST,
                gobject.TYPE_NONE,
                (gobject.TYPE_PYOBJECT, ))}

    def __init__(self, function, *args, **kwargs):
        threading.Thread.__init__(self)
        _IdleObject.__init__(self)
        self._function = function
        self._args = args
        self._kwargs = kwargs

    def run(self):
        # call the function
        _log.debug('Thread %s calling %s' % (self.name, str(self._function)))

        args = self._args
        kwargs = self._kwargs

        try:
            result = self._function(*args, **kwargs)
        except Exception, exc:
            self.emit("exception", exc)
            return

        _log.debug('Thread %s completed' % (self.name))

        self.emit("completed", result)
        return


class _ThreadManager(object):
    """
    Manager to add new threads and remove finished ones from queue
    """

    def __init__(self, max_threads=4):
        """
        Start the thread pool. The number of threads in the pool is defined
        by `pool_size`, defaults to 4
        """

        self._max_threads = max_threads
        self._thread_pool = []
        self._running = []
        self._thread_id = 0

        return

    def _remove_thread(self, widget, arg=None):
        """
        Called when the thread completes. Remove it from the thread list
        and start the next thread (if there is any)
        """

        # not actually a widget. It's the object that emitted the signal, in
        # this case, the _WorkerThread object.
        thread_id = widget.name

        _log.debug('Thread %s completed, %d threads in the queue' % (thread_id,
                len(self._thread_pool)))

        self._running.remove(thread_id)

        if self._thread_pool:
            if len(self._running) < self._max_threads:
                next = self._thread_pool.pop()
                _log.debug('Dequeuing thread %s', next.name)
                self._running.append(next.name)
                next.start()

        return

    def add_work(self, complete_cb, exception_cb, func, *args, **kwargs):
        """
        Add a work to the thread list
        complete_cb: function to call when 'func' finishes
        exception_cb: function to call when 'func' rises an exception
        func: function to do the main part of the work
        *args, **kwargs: arguments for func
        """

        thread = _WorkerThread(func, *args, **kwargs)
        thread_id = '%s' % (self._thread_id)

        thread.connect('completed', complete_cb)
        thread.connect('completed', self._remove_thread)
        thread.connect('exception', exception_cb)
        thread.setName(thread_id)

        if len(self._running) < self._max_threads:
            self._running.append(thread_id)
            thread.start()
        else:
            running_names = ', '.join(self._running)
            _log.debug('Threads %s running, adding %s to the queue',
                    running_names, thread_id)
            self._thread_pool.append(thread)

        self._thread_id += 1
        return


class MainWindow:
    """
    The main application interface, GUI and Facebook interfacing functions
    """


    #------------------------------
    # Information sending functions
    #------------------------------
    def sendupdate(self):
        """
        Sending status update to FB, if the user entered any
        """

        textfield = self.entry.get_buffer()
        start = textfield.get_start_iter()
        end = textfield.get_end_iter()
        entry_text = textfield.get_text(start, end)
        if entry_text != "":
            # Warn user if status message is too long. If insist, send text
            if len(entry_text) > MAX_MESSAGE_LENGTH:
                warning_message = ("Your message is longer than %d " \
                    "characters and if submitted it is likely to be " \
                    "truncated by Facebook as:\n\"%s...\"\nInstead of " \
                    "sending this update, do you want to return to editing?" \
                    % (MAX_MESSAGE_LENGTH, entry_text[0:251]))
                warning_dialog = gtk.MessageDialog(parent=self.window,
                    type=gtk.MESSAGE_WARNING,
                    flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                    message_format="Your status update is too long.",
                    buttons=gtk.BUTTONS_YES_NO)
                warning_dialog.format_secondary_text(warning_message)
                response = warning_dialog.run()
                warning_dialog.destroy()
                # If user said yes, don't send just return to editing
                if response == gtk.RESPONSE_YES:
                    return

            _log.info('Sending status update: %s\n' % entry_text)
            self.statusbar.pop(self.statusbar_context)
            self.statusbar.push(self.statusbar_context, \
                "Sending your status update")
            self._facebook.status.set([entry_text], [self._facebook.uid])

            # Empty entry field and status bar
            textfield.set_text("")
            self.statusbar.pop(self.statusbar_context)

            # wait a little before getting new updates, so FB can catch up
            time.sleep(2)
            self.refresh()

    #------------------------------
    # Information pulling functions
    #------------------------------
    def get_friends_list(self):
        """
        Fetching list of friends' names (and the current user's) to
        store and use when status updates are displayed
        Threading callbacks: post_get_friends_list, except_get_friends_list
        """

        self.statusbar.pop(self.statusbar_context)
        self.statusbar.push(self.statusbar_context, \
            "Fetching list of friends")
        query = ("SELECT uid, name, pic_square FROM user "\
            "WHERE (uid IN (SELECT uid2 FROM friend WHERE uid1 = %d) "\
            "OR uid = %d)" % (self._facebook.uid, self._facebook.uid))
        _log.debug("Friend list query: %s" % (query))
        friends = self._facebook.fql.query([query])
        return friends

    def post_get_friends_list(self, widget, results):
        """
        Callback function when friends list is successfully pulled
        Makes dictionary of uid->friendsname, and pulls new statuses
        """

        friends = results
        for friend in friends:
            # In "friend" table UID is integer
            self.friendsname[str(friend['uid'])] = friend['name']
            self.friendsprofilepic[str(friend['uid'])] = \
                friend['pic_square']

        # Not all friends can be pulled, depends on their privacy settings
        _log.info('%s has altogether %d friends in the database.' \
            % (self.friendsname[str(self._facebook.uid)],
            len(self.friendsname.keys())))
        self.statusbar.pop(self.statusbar_context)

        self.refresh()
        return

    def except_get_friends_list(self, widget, exception):
        """
        Callback if there's an exception raised while fetching friends' names
        """

        _log.error("Get friends exception: %s" % (str(exception)))
        self.statusbar.pop(self.statusbar_context)
        self.statusbar.push(self.statusbar_context, \
            "Error while fetching friends' list")

    def get_status_list(self):
        """
        Fetching new statuses using FQL query for user's friends (and
        their own) between last fetch and now
        Threading callbacks: post_get_status_list, except_get_status_list
        """

        # Halt point, only one status update may proceed at a time
        # .release() is called at all 3 possible update finish:
        # except_get_status_list, _post_get_cl_list, _except_get_cl_list
        self.update_sema.acquire()

        self.statusbar.pop(self.statusbar_context)
        self.statusbar.push(self.statusbar_context, \
            "Fetching status updates")

        # If not first update then get new statuses since then
        # otherwise get them since 5 days ago (i.e. long time ago)
        if self._last_update > 0:
            since = self._last_update
        else:
            now = int(time.time())
            since = now - 5*24*60*60
        till = int(time.time())

        _log.info("Fetching status updates published between %s and %s" \
            % (time.strftime("%c", time.localtime(since)),
            time.strftime("%c", time.localtime(till))))

        # User "stream" table to get status updates because the older "status"
        # has too many bugs and limitations
        # Status update is a post that has no attachment nor target
        query = ("SELECT source_id, created_time, post_id, message " \
            "FROM stream "\
            "WHERE ((source_id IN (SELECT uid2 FROM friend WHERE uid1 = %d) "\
            "OR source_id = %d) "\
            "AND created_time  > %d AND created_time < %d "\
            "AND attachment = '' AND target_id = '') "\
            "ORDER BY created_time DESC "\
            "LIMIT 100"
            % (self._facebook.uid, self._facebook.uid, since, till))
        _log.debug('Status list query: %s' % (query))

        status = self._facebook.fql.query([query])

        _log.info('Received %d new status' % (len(status)))
        return [status, till]

    def post_get_status_list(self, widget, results):
        """
        Callback function when new status updates are successfully pulled
        Adds statuses to listview and initiates pulling comments & likes
        restults: [status_updates_array, till_time]
        """

        _log.debug('Status updates successfully pulled.')
        updates = results[0]
        till = results[1]

        # There are new updates
        if len(updates) > 0:
            updates.reverse()
            for up in updates:
                # source_id is the UID, and in "stream" it is string, not int
                self.liststore.prepend((up['post_id'],
                    up['source_id'],
                    up['message'],
                    up['created_time'],
                    '0',
                    '0'))
            # Scroll to newest status in view
            model = self.treeview.get_model()
            first_iter = model.get_iter_first()
            first_path = model.get_path(first_iter)
            self.treeview.scroll_to_cell(first_path)

        self.statusbar.pop(self.statusbar_context)

        # pull comments and likes too
        self._threads.add_work(self._post_get_cl_list,
            self._except_get_cl_list,
            self._get_cl_list,
            till)
        return

    def except_get_status_list(self, widget, exception):
        """
        Callback if there's an exception raised while fetching status list
        """

        _log.error("Get status list exception: %s" % (str(exception)))
        self.statusbar.pop(self.statusbar_context)
        self.statusbar.push(self.statusbar_context, \
            "Error while fetching status updates")
        # Finish, give semaphore back in case anyone's waiting
        self.update_sema.release()

    ### image download function
    def _dl_profile_pic(self, uid, url):
        """
        Download user profile pictures
        Threading callbacks: _post_dl_profile_pic, _exception_dl_profile_pic
        url: picture's url
        """

        request = urllib2.Request(url=url)
        _log.debug('Starting request of %s' % (url))
        response = urllib2.urlopen(request)
        data = response.read()
        _log.debug('Request completed')

        return (uid, data)

    ### Results from the picture request
    def _post_dl_profile_pic(self, widget, data):
        """
        Callback when profile picture is successfully downloaded
        Replaces default picture with the users profile pic in status list
        """

        (uid, data) = data

        loader = gtk.gdk.PixbufLoader()
        loader.write(data)
        loader.close()

        user_pic = loader.get_pixbuf()
        # Replace default picture
        self._profilepics[uid] = user_pic

        # Redraw to get new picture
        self.treeview.queue_draw()
        return

    def _exception_dl_profile_pic(self, widget, exception):
        """
        Callback when there's an excetion during downloading profile picture
        """

        _log.debug('Exception trying to get a profile picture.')
        _log.debug(str(exception))
        return

    ### get comments and likes
    def _get_cl_list(self, till):
        """
        Fetch comments & likes for the listed statuses
        Threading callbacks: _post_get_cl_list, _except_get_cl_list
        till: time between self.last_update and till
        """

        _log.info('Pulling comments & likes for listed status updates')
        self.statusbar.pop(self.statusbar_context)
        self.statusbar.push(self.statusbar_context, \
            "Fetching comments & likes")

        # Preparing list of status update post_id for FQL query
        post_id = []
        for row in self.liststore:
            post_id.append('post_id = "%s"' % (row[Columns.STATUSID]))
        all_id = ' OR '.join(post_id)

        query = ('SELECT post_id, comments, likes FROM stream WHERE ((%s) ' \
            'AND updated_time > %d AND updated_time < %d)' % \
            (all_id, self._last_update, till))
        _log.debug('Comments & Likes query: %s' % (query))

        cl_list = self._facebook.fql.query([query])

        return (cl_list, till)

    ### Results from the picture request
    def _post_get_cl_list(self, widget, data):
        """
        Callback when successfully fetched new comments and likes
        Ends up here if complete 'refresh' is successfull
        """

        list = data[0]
        till = data[1]

        likes_list = {}
        comments_list = {}

        for item in list:
            status_id = item['post_id']
            likes_list[status_id] = str(item['likes']['count'])
            comments_list[status_id] = str(item['comments']['count'])

        for row in self.liststore:
            rowstatus = row[Columns.STATUSID]
            # have to check if post really exists, deleted post still
            # show up in "status" table sometimes, not sure in "stream"
            if rowstatus in likes_list.keys():
                row[Columns.LIKES] = likes_list[rowstatus]
                row[Columns.COMMENTS] = comments_list[rowstatus]
            else:
                _log.debug("Possible deleted status update: " \
                "uid: %s, status_id: %s, user: %s, text: %s, time: %s" \
                % (row[Columns.UID], rowstatus, \
                self.friendsname[str(row[Columns.UID])], \
                row[Columns.STATUS], row[Columns.DATETIME]))

        # Update time of last update since this finished just fine
        self._last_update = till
        _log.info('Finished updating status messages, comments and likes.')
        self.statusbar.pop(self.statusbar_context)

        # Last update time in human readable format
        update_time = time.strftime("%H:%M", time.localtime(till))
        self.statusbar.push(self.statusbar_context, \
            "Last update at %s" % (update_time))

        # Finish, give semaphore back in case anyone's waiting
        self.update_sema.release()
        return

    def _except_get_cl_list(self, widget, exception):
        """
        Callback if there' an exception during comments and likes fetch
        """

        _log.error('Exception while getting comments and likes')
        _log.error(str(exception))
        self.statusbar.pop(self.statusbar_context)
        self.statusbar.push(self.statusbar_context, \
            "Error while fetching comments & likes")
        # Finish, give semaphore back in case anyone's waiting
        self.update_sema.release()
        return

    #-----------------
    # Helper functions
    #-----------------
    def count(self, text):
        """
        Count remaining characters in status update text
        """

        start = text.get_start_iter()
        end = text.get_end_iter()
        thetext = text.get_text(start, end)
        self.count_label.set_text('(%d)' \
            % (MAX_MESSAGE_LENGTH - len(thetext)))
        return True

    def set_auto_refresh(self):
        """
        Enable auto refresh statuses in pre-defined intervals
        """

        if self._refresh_id:
            gobject.source_remove(self._refresh_id)

        self._refresh_id = gobject.timeout_add(
                self._prefs['auto_refresh_interval']*60*1000,
                self.refresh)
        _log.info("Auto-refresh enabled: %d minutes" \
            % (self._prefs['auto_refresh_interval']))

    def refresh(self, widget=None):
        """
        Queueing refresh in thread pool, subject to semaphores
        """

        _log.info('Queueing refresh now at %s' % (time.strftime('%H:%M:%S')))
        self._threads.add_work(self.post_get_status_list,
            self.except_get_status_list,
            self.get_status_list)
        return True

    def status_format(self, column, cell, store, position):
        """
        Format how status update should look in list
        """

        uid = store.get_value(position, Columns.UID)
        name = self.friendsname[str(uid)]
        status = store.get_value(position, Columns.STATUS)
        posttime = store.get_value(position, Columns.DATETIME)

        #replace characters that would choke the markup
        status = re.sub(r'&', r'&amp;', status)
        status = re.sub(r'<', r'&lt;', status)
        status = re.sub(r'>', r'&gt;', status)
        markup = ('<b>%s</b> %s\n(%s ago)' % \
                (name, status, timesince.timesince(posttime)))
        _log.debug('Marked up text: %s' % (markup))
        cell.set_property('markup', markup)
        return

    def open_url(self, source, url):
        """
        Open url as new browser tab
        """

        _log.debug('Opening url: %s' % url)
        import webbrowser
        webbrowser.open_new_tab(url)
        self.window.set_focus(self.entry)

    def copy_status_to_clipboard(self, source, text):
        """
        Copy current status (together with poster name but without time)
        to the clipboard
        """

        clipboard = gtk.Clipboard()
        _log.debug('Copying to clipboard: %s' % (text))
        clipboard.set_text(text)

    #--------------------
    # Interface functions
    #--------------------
    def quit(self, widget):
        """
        Finish program
        """
        gtk.main_quit()

    def systray_click(self, widget, user_param=None):
        """
        Callback when systray icon receives left-click
        """

        # Toggle visibility of main window
        if self.window.get_property('visible'):
            _log.debug('Hiding window')
            x, y = self.window.get_position()
            self._prefs['window_pos_x'] = x
            self._prefs['window_pos_y'] = y
            self.window.hide()
        else:
            x = self._prefs['window_pos_x']
            y = self._prefs['window_pos_y']
            _log.debug('Restoring window at (%d, %d)' % (x, y))
            self.window.move(x, y)
            self.window.deiconify()
            self.window.present()

    def create_grid(self):
        """
        Create list where each line consist of:
        profile pic, status update, comments and likes count
        """

        # List for storing all relevant info
        self.liststore = gtk.ListStore(gobject.TYPE_STRING,
            gobject.TYPE_STRING,
            gobject.TYPE_STRING,
            gobject.TYPE_INT,
            gobject.TYPE_STRING,
            gobject.TYPE_STRING)

        # Short items by time, newest first
        self.sorter = gtk.TreeModelSort(self.liststore)
        self.sorter.set_sort_column_id(Columns.DATETIME, gtk.SORT_DESCENDING)
        self.treeview = gtk.TreeView(self.sorter)

        # No headers
        self.treeview.set_property('headers-visible', False)
        # Alternating background colours for lines
        self.treeview.set_rules_hint(True)

        # Column showing profile picture
        profilepic_renderer = gtk.CellRendererPixbuf()
        profilepic_column = gtk.TreeViewColumn('Profilepic', \
            profilepic_renderer)
        profilepic_column.set_fixed_width(55)
        profilepic_column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
        profilepic_column.set_cell_data_func(profilepic_renderer,
                self._cell_renderer_profilepic)
        self.treeview.append_column(profilepic_column)

        # Column showing status text
        self.status_renderer = gtk.CellRendererText()
        # wrapping: pango.WRAP_WORD == 0, don't need to import pango for that
        self.status_renderer.set_property('wrap-mode', 0)
        self.status_renderer.set_property('wrap-width', 320)
        self.status_renderer.set_property('width', 320)
        self.status_column = gtk.TreeViewColumn('Message', \
                self.status_renderer, text=1)
        self.status_column.set_cell_data_func(self.status_renderer, \
                self.status_format)
        self.treeview.append_column(self.status_column)

        # Showing the number of comments
        comments_renderer = gtk.CellRendererText()
        comments_column = gtk.TreeViewColumn('Comments', \
                comments_renderer, text=1)
        comments_column.set_cell_data_func(comments_renderer, \
                self._cell_renderer_comments)
        self.treeview.append_column(comments_column)

        # Showing the comments icon
        commentspic_renderer = gtk.CellRendererPixbuf()
        commentspic_column = gtk.TreeViewColumn('CommentsPic', \
                commentspic_renderer)
        commentspic_column.set_cell_data_func(commentspic_renderer, \
                self._cell_renderer_commentspic)
        commentspic_column.set_fixed_width(28)
        commentspic_column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
        self.treeview.append_column(commentspic_column)

        # Showing the number of likes
        likes_renderer = gtk.CellRendererText()
        likes_column = gtk.TreeViewColumn('Likes', \
                likes_renderer, text=1)
        likes_column.set_cell_data_func(likes_renderer, \
                self._cell_renderer_likes)
        self.treeview.append_column(likes_column)

        # Showing the likes icon
        likespic_renderer = gtk.CellRendererPixbuf()
        likespic_column = gtk.TreeViewColumn('Likespic', \
                likespic_renderer)
        likespic_column.set_cell_data_func(likespic_renderer, \
                self._cell_renderer_likespic)
        likespic_column.set_fixed_width(28)
        likespic_column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
        self.treeview.append_column(likespic_column)

        self.treeview.set_resize_mode(gtk.RESIZE_IMMEDIATE)

        self.treeview.connect('row-activated', self.open_status_web)
        self.treeview.connect('button-press-event', self.click_status)

    def create_menubar(self):
        """
        Showing the app's (very basic) menubar
        """

        refresh_action = gtk.Action('Refresh', '_Refresh',
                'Get new status updates', gtk.STOCK_REFRESH)
        refresh_action.connect('activate', self.refresh)

        quit_action = gtk.Action('Quit', '_Quit',
                'Exit %s' % (APPNAME), gtk.STOCK_QUIT)
        quit_action.connect('activate', self.quit)

        about_action = gtk.Action('About', '_About', 'About %s' % (APPNAME),
                gtk.STOCK_ABOUT)
        about_action.connect('activate', self.show_about)

        self.action_group = gtk.ActionGroup('MainMenu')
        self.action_group.add_action_with_accel(refresh_action, 'F5')
        # accel = None to use the default acceletator
        self.action_group.add_action_with_accel(quit_action, None)
        self.action_group.add_action(about_action)

        uimanager = gtk.UIManager()
        uimanager.insert_action_group(self.action_group, 0)
        ui = '''
        <ui>
          <menubar name="MainMenu">
            <menuitem action="Quit" />
            <separator />
            <menuitem action="Refresh" />
            <separator />
            <menuitem action="About" />
          </menubar>
       </ui>
        '''
        uimanager.add_ui_from_string(ui)
        self.main_menu = uimanager.get_widget('/MainMenu')
        return

    def show_about(self, widget):
        """
        Show the about dialog
        """

        about_window = gtk.AboutDialog()
        about_window.set_name(APPNAME)
        about_window.set_version(VERSION)
        about_window.set_copyright('2009 Gergely Imreh')
        about_window.set_license(MIT)
        about_window.set_website('http://imrehg.github.com/minibook/')
        about_window.set_website_label('%s on GitHub' % (APPNAME))
        about_window.set_authors(['Gergely Imreh'])
        about_window.connect('close', self.close_dialog)
        about_window.run()
        about_window.hide()

    def close_dialog(self, user_data=None):
        """
        Hide the dialog window
        """

        return True

    def open_status_web(self, treeview, path, view_column, user_data=None):
        """
        Callback to open status update in web browser when received left click
        """

        model = treeview.get_model()
        if not model:
            return

        iter = model.get_iter(path)
        uid = model.get_value(iter, Columns.UID)
        status_id = model.get_value(iter, Columns.STATUSID).split("_")[1]
        status_url = ('http://www.facebook.com/profile.php?' \
            'id=%s&v=feed&story_fbid=%s' % (uid, status_id))
        self.open_url(path, status_url)
        return

    def click_status(self, treeview, event, user_data=None):
        """
        Callback when a mouse click event occurs on one of the rows
        """

        _log.debug('Clicked on status list')
        if event.button != 3:
            # Only right clicks are processed
            return False
        _log.debug('right-click received')

        x = int(event.x)
        y = int(event.y)

        pth = treeview.get_path_at_pos(x, y)
        if not pth:
            return False

        path, col, cell_x, cell_y = pth
        treeview.grab_focus()
        treeview.set_cursor(path, col, 0)

        self.show_status_popup(treeview, event)
        return True

    def show_status_popup(self, treeview, event, user_data=None):
        """
        Show popup menu relevant to the clicked status update
        """

        _log.debug('Show popup menu')
        cursor = treeview.get_cursor()
        if not cursor:
            return
        model = treeview.get_model()
        if not model:
            return

        path = cursor[0]
        iter = model.get_iter(path)

        popup_menu = gtk.Menu()
        popup_menu.set_screen(self.window.get_screen())

        open_menu_items = []

        # Open this status update in browser
        uid = model.get_value(iter, Columns.UID)
        status_id = model.get_value(iter, Columns.STATUSID).split("_")[1]
        url = ('http://www.facebook.com/profile.php?' \
            'id=%s&v=feed&story_fbid=%s' % (uid, status_id))
        item_name = 'This status'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        # Open user's wall in browser
        url = ('http://www.facebook.com/profile.php?' \
            'id=%s' % (uid))
        item_name = 'User wall'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        # Open user's info in browser
        url = ('http://www.facebook.com/profile.php?' \
            'id=%s&v=info' % (uid))
        item_name = 'User info'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        # Open user's photos in browser
        url = ('http://www.facebook.com/profile.php?' \
            'id=%s&v=photos' % (uid))
        item_name = 'User photos'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        open_menu = gtk.Menu()
        for item in open_menu_items:
            open_menu.append(item)

        # Menu item for "open in browser" menu
        open_item = gtk.ImageMenuItem('Open in browser')
        open_item.get_image().set_from_stock(gtk.STOCK_GO_FORWARD, \
            gtk.ICON_SIZE_MENU)
        open_item.set_submenu(open_menu)
        popup_menu.append(open_item)

        # Menu item to copy status message to clipboard
        message = model.get_value(iter, Columns.STATUS)
        name = self.friendsname[str(uid)]
        text = ("%s %s" % (name, message))
        copy_item = gtk.ImageMenuItem('Copy status')
        copy_item.get_image().set_from_stock(gtk.STOCK_COPY, \
            gtk.ICON_SIZE_MENU)
        copy_item.connect('activate', self.copy_status_to_clipboard, text)
        popup_menu.append(copy_item)

        popup_menu.show_all()

        if event:
            b = event.button
            t = event.time
        else:
            b = 1
            t = 0

        popup_menu.popup(None, None, None, b, t)

        return True

    def _cell_renderer_profilepic(self, column, cell, store, position):
        """
        Showing profile picture in status update list
        Use default picture if we don't (can't) have the user's
        If first time trying to display it try to download and display default
        """

        uid = str(store.get_value(position, Columns.UID))
        if not uid in self._profilepics:
            profilepicurl = self.friendsprofilepic[uid]
            if profilepicurl:
                _log.debug('%s does not have profile picture stored, ' \
                    'queuing fetch from %s' % (uid, profilepicurl))
                self._threads.add_work(self._post_dl_profile_pic,
                    self._exception_dl_profile_pic,
                    self._dl_profile_pic,
                    uid,
                    profilepicurl)
            else:
                _log.debug('%s does not have profile picture set, ' % (uid))

            self._profilepics[uid] = self._default_profilepic

        cell.set_property('pixbuf', self._profilepics[uid])

        return

    def _cell_renderer_comments(self, column, cell, store, position):
        """
        Cell renderer for the number of comments
        """

        comments = int(store.get_value(position, Columns.COMMENTS))
        if comments > 0:
            cell.set_property('text', str(comments))
        else:
            cell.set_property('text', '')

    def _cell_renderer_commentspic(self, column, cell, store, position):
        """
        Cell renderer for comments picture if there are any comments
        """

        comments = int(store.get_value(position, Columns.COMMENTS))
        if comments > 0:
            cell.set_property('pixbuf', self.commentspic)
        else:
            cell.set_property('pixbuf', None)

    def _cell_renderer_likes(self, column, cell, store, position):
        """
        Cell renderer for number of likes
        """

        likes = int(store.get_value(position, Columns.LIKES))
        if likes > 0:
            cell.set_property('text', str(likes))
        else:
            cell.set_property('text', '')

    def _cell_renderer_likespic(self, column, cell, store, position):
        """
        Cell renderer for likess picture if there are any likes
        """

        likes = int(store.get_value(position, Columns.LIKES))
        if likes > 0:
            cell.set_property('pixbuf', self.likespic)
        else:
            cell.set_property('pixbuf', None)

    #------------------
    # Main Window start
    #------------------
    def __init__(self, facebook):
        """
        Creating main window and setting up relevant variables
        """

        global spelling_support

        # Connect to facebook object
        self._facebook = facebook

        # Picture shown if cannot get a user's own profile picture
        unknown_user = 'pixmaps/unknown_user.png'
        if unknown_user:
            self._default_profilepic = gtk.gdk.pixbuf_new_from_file(
                    unknown_user)
        else:
            self._default_profilepic = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB,
                    has_alpha=False, bits_per_sample=8, width=50, height=50)

        # Icons for "comments" and "likes"
        self.commentspic = gtk.gdk.pixbuf_new_from_file('pixmaps/comments.png')
        self.likespic = gtk.gdk.pixbuf_new_from_file('pixmaps/likes.png')

        # create a new window
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_size_request(480, 250)
        self.window.set_title("Minibook")
        self.window.connect("delete_event", lambda w, e: gtk.main_quit())

        vbox = gtk.VBox(False, 0)
        self.window.add(vbox)

        self.friendsname = {}
        self.friendsprofilepic = {}
        self._profilepics = {}
        # Semaphore to let only one status update proceed at a time
        self.update_sema = threading.BoundedSemaphore(value=1)

        # Menubar
        self.create_menubar()
        vbox.pack_start(self.main_menu, False, True, 0)

        # Status update display window
        self.create_grid()
        self.statuslist_window = gtk.ScrolledWindow()
        self.statuslist_window.set_policy(gtk.POLICY_NEVER, gtk.POLICY_ALWAYS)
        self.statuslist_window.add(self.treeview)
        vbox.pack_start(self.statuslist_window, True, True, 0)

        # Area around the status update entry box with labels and button
        label_box = gtk.HBox(False, 0)
        label = gtk.Label("What's on your mind?")
        self.count_label = gtk.Label("(%d)" % (MAX_MESSAGE_LENGTH))
        label_box.pack_start(label)
        label_box.pack_start(self.count_label)

        self.entry = gtk.TextView()
        text = self.entry.get_buffer()
        text.connect('changed', self.count)
        text_box = gtk.VBox(True, 0)
        text_box.pack_start(label_box)
        text_box.pack_start(self.entry, True, True, 4)

        update_button = gtk.Button(stock=gtk.STOCK_ADD)
        update_button.connect("clicked", lambda w: self.sendupdate())

        update_box = gtk.HBox(False, 0)
        update_box.pack_start(text_box, expand=True, fill=True,
                padding=0)
        update_box.pack_start(update_button, expand=False, fill=False,
                padding=0)

        vbox.pack_start(update_box, False, True, 0)

        # Statusbar
        self.statusbar = gtk.Statusbar()
        vbox.pack_start(self.statusbar, False, False, 0)
        self.statusbar_context = self.statusbar.get_context_id(
                '%s is here.' % (APPNAME))

        # Set up spell checking if it is available
        if spelling_support:
            try:
                spelling = gtkspell.Spell(self.entry, 'en')
            except:
                spelling_support = False

        # Show window
        self.window.show_all()

        # Set up systray icon
        self._app_icon = 'pixmaps/minibook.png'
        self._systray = gtk.StatusIcon()
        self._systray.set_from_file(self._app_icon)
        self._systray.set_tooltip('%s\n' \
            'Left-click: toggle window hiding' % (APPNAME))
        self._systray.connect('activate', self.systray_click)
        self._systray.set_visible(True)

        self.window.set_icon_from_file(self._app_icon)

        # Enable thread manager
        self._threads = _ThreadManager()

        self.userinfo = self._facebook.users.getInfo([self._facebook.uid], \
            ['name'])[0]
        self._last_update = 0
        self._threads.add_work(self.post_get_friends_list,
                self.except_get_friends_list,
                self.get_friends_list)

        # Start to set up preferences
        self._prefs = {}
        x, y = self.window.get_position()
        self._prefs['window_pos_x'] = x
        self._prefs['window_pos_y'] = y
        self._prefs['auto_refresh_interval'] = 5

        # Enable auto-refresh
        self._refresh_id = None
        self.set_auto_refresh()


def main(facebook):
    """
    Main function
    """

    gtk.main()
    gtk.gdk.threads_leave()
    _log.debug('Exiting')
    return 0

if __name__ == "__main__":
    """
    Set up facebook object, login and start main window
    """

    # Currently cannot include the registered app's
    # api_key and secret_key, thus have to save them separately
    # Here those keys are loaded
    try:
        config_file = open("config", "r")
        api_key = config_file.readline()[:-1]
        secret_key = config_file.readline()[:-1]
        _log.debug('Config file loaded successfully')
    except Exception, e:
        _log.critical('Error while loading config file: %s' % (str(e)))
        exit(1)

    facebook = Facebook(api_key, secret_key)
    try:
        facebook.auth.createToken()
    except:
        # Like catch errors like
        # http://bugs.developers.facebook.com/show_bug.cgi?id=5474
        # and http://bugs.developers.facebook.com/show_bug.cgi?id=5472
        _log.critical("Error on Facebook's side, " \
            "try starting application later")
        exit(1)

    facebook.login()
    _log.debug('Showing Facebook login page in default browser.')

    # Delay dialog to allow for login in browser
    got_session = False
    while not got_session:
        dia = gtk.Dialog('minibook: login',
            None,
            gtk.DIALOG_MODAL | \
            gtk.DIALOG_DESTROY_WITH_PARENT | \
            gtk.DIALOG_NO_SEPARATOR,
            ("Logged in", gtk.RESPONSE_OK, \
            gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL))
        label = gtk.Label("%s is opening your web browser to " \
            "log in Facebook.\nWhen finished, click 'Logged in', " \
            "or you can cancel now." % (APPNAME))
        dia.vbox.pack_start(label, True, True, 10)
        label.show()
        dia.show()
        result = dia.run()
        # Cancel login and close app
        if result == gtk.RESPONSE_CANCEL:
            _log.debug('Exiting before Facebook login.')
            exit(0)
        dia.destroy()
        try:
            facebook.auth.getSession()
            got_session = True
        except:
            # Likely clicked "logged in" but not logged in yet, start over
            pass

    _log.info('Session Key: %s' % (facebook.session_key))
    _log.info('User\'s UID: %d' % (facebook.uid))

    # Start main window and app
    MainWindow(facebook)
    main(facebook)
