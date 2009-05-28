#!/usr/bin/env python
""" Minibook: the Facebook(TM) status updater
(C) 2009 Gergely Imreh <imrehg@gmail.com>
"""

VERSION = '0.1.0'
APPNAME = 'minibook'

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
    """A single working thread."""

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
            _log.error('Exception %s' % str(exc))
            self.emit("exception", exc)
            return

        _log.debug('Thread %s completed' % (self.name))

        self.emit("completed", result)
        return


class _ThreadManager(object):
    """Manages the threads."""

    def __init__(self, max_threads=2):
        """Start the thread pool. The number of threads in the pool is defined
        by `pool_size`, defaults to 2."""
        self._max_threads = max_threads
        self._thread_pool = []
        self._running = []
        self._thread_id = 0

        return

    def _remove_thread(self, widget, arg=None):
        """Called when the thread completes. We remove it from the thread list
        (dictionary, actually) and start the next thread (if there is one)."""

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
        """Add a work to the thread list."""

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
    """The main application interface"""


    #------------------------------
    # Information sending functions
    #------------------------------
    def sendupdate(self):
        textfield = self.entry.get_buffer()
        start = textfield.get_start_iter()
        end = textfield.get_end_iter()
        entry_text = textfield.get_text(start, end)
        if entry_text != "":
            _log.info('Sent status update: %s\n' % entry_text)
            self._facebook.status.set([entry_text], [self._facebook.uid])

            textfield.set_text("")
            # wait a little before getting new updates, so FB can catch up
            time.sleep(2)
            self.refresh()

    #------------------------------
    # Information pulling functions
    #------------------------------
    def get_friends_list(self):
        query = ("SELECT uid, name, pic_square FROM user \
            WHERE (uid IN (SELECT uid2 FROM friend WHERE uid1 = %d) \
            OR uid = %d)" % (self._facebook.uid, self._facebook.uid))
        friends = self._facebook.fql.query([query])
        return friends

    def post_get_friends_list(self, widget, results):
        friends = results
        for friend in friends:
            self.friendsname[str(friend['uid'])] = friend['name']
            self.friendsprofilepic[str(friend['uid'])] = \
                friend['pic_square']

        _log.info('%s has altogether %d friends in the database.' \
            % (self.friendsname[str(self._facebook.uid)],
            len(self.friendsname.keys())))
        self.refresh()
        return

    def except_get_friends_list(self, widget, exception):
        _log.error("Get friends exception: %s" % (str(exception)))

    def get_status_list(self):
        if self._last_update > 0:
            since = self._last_update
        else:
            now = int(time.time())
            since = now - 5*24*60*60
        till = int(time.time())

        _log.info("Fetching status updates published between %s and %s" \
            % (time.strftime("%c", time.localtime(since)),
            time.strftime("%c", time.localtime(till))))

        query = ('SELECT uid, time, status_id, message FROM status \
            WHERE (uid IN (SELECT uid2 FROM friend WHERE uid1 = %d) \
            OR uid = %d) \
            AND time  > %d AND time < %d\
            ORDER BY time DESC\
            LIMIT 60' \
            % (self._facebook.uid, self._facebook.uid, since, till))
        _log.debug('Status list query: %s' % (query))

        status = self._facebook.fql.query([query])

        _log.info('Received %d new status' % (len(status)))
        return [status, till]

    def post_get_status_list(self, widget, results):
        _log.debug('Status updates successfully pulled.')
        updates = results[0]
        self._last_update = results[1]

        # There are no updates
        if len(updates) == 0:
            return

        # There are new updates
        updates.reverse()
        for up in updates:
            self.liststore.prepend((up['status_id'],
                up['uid'],
                up['message'],
                up['time'],
                '0',
                '0'))
        # Scroll to latest status in view
        model = self.treeview.get_model()
        first_iter = model.get_iter_first()
        first_path = model.get_path(first_iter)
        self.treeview.scroll_to_cell(first_path)
        self._threads.add_work(self._post_get_cl_list,
            self._except_get_cl_list,
            self._get_cl_list)
        return

    def except_get_status_list(self, widget, exception):
        _log.error("Get status list exception: %s" % (str(exception)))

    ### image download function
    def _dl_profile_pic(self, uid, url):
        request = urllib2.Request(url=url)
        _log.debug('Starting request of %s' % (url))
        response = urllib2.urlopen(request)
        data = response.read()
        _log.debug('Request completed')

        return (uid, data)

    ### Results from the picture request
    def _post_dl_profile_pic(self, widget, data):
        (uid, data) = data

        loader = gtk.gdk.PixbufLoader()
        loader.write(data)
        loader.close()

        user_pic = loader.get_pixbuf()
        self._profilepics[uid] = user_pic

        self.treeview.queue_draw()
        return

    def _exception_dl_profile_pic(self, widget, exception):
        _log.debug('Exception trying to get a profile picture.')
        _log.debug(str(exception))
        return

    ### get comments and likes 
    def _get_cl_list(self):
        _log.info('Pulling comments & likes for listed status updates')
        post_id = []
        for row in self.liststore:
            post_id.append('post_id = "%d_%s"' % (row[Columns.UID], \
                row[Columns.STATUSID]))
        all_id = ' OR '.join(post_id)
        query = ('SELECT post_id, comments, likes FROM stream WHERE (%s)' % \
            (all_id))
        _log.debug('Comments & Likes query: %s' % (query))
        cl_list = self._facebook.fql.query([query])
        return(cl_list)

    ### Results from the picture request
    def _post_get_cl_list(self, widget, data):
        likes_list = {}
        comments_list = {}
        for item in data:
            status_id = item['post_id'].split("_")[1]
            likes_list[status_id] = str(item['likes']['count'])
            comments_list[status_id] = str(item['comments']['count'])
        for row in self.liststore:
            rowstatus = row[Columns.STATUSID]
            row[Columns.LIKES] = likes_list[rowstatus]
            row[Columns.COMMENTS] = comments_list[rowstatus]
        return

    def _except_get_cl_list(self, widget, exception):
        _log.error('Exception while getting comments and likes')
        _log.error(str(exception))
        return

    #-----------------
    # Helper functions
    #-----------------
    def count(self, text):
        start = text.get_start_iter()
        end = text.get_end_iter()
        thetext = text.get_text(start, end)
        self.count_label.set_text('(%d)' % (160 - len(thetext)))
        return True

    def set_auto_refresh(self):
        if self._refresh_id:
            gobject.source_remove(self._refresh_id)

        self._refresh_id = gobject.timeout_add(
                self._prefs['auto_refresh_interval']*60*1000,
                self.refresh)
        _log.info("Auto-refresh enabled: %d minutes" \
            % (self._prefs['auto_refresh_interval']))

    def refresh(self):
        _log.info('Refreshing now at %s' % (time.strftime('%H:%M:%S')))
        self._threads.add_work(self.post_get_status_list,
            self.except_get_status_list,
            self.get_status_list)
        return True

    def status_format(self, column, cell, store, position):
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
        """Open url as new browser tab."""
        _log.debug('Opening url: %s' % url)
        import webbrowser
        webbrowser.open_new_tab(url)
        self.window.set_focus(self.entry)
        
    def copy_status_to_clipboard(self, source, text):
        clipboard = gtk.Clipboard()
        _log.debug('Copying to clipboard: %s' % (text))
        clipboard.set_text(text)

    #--------------------
    # Interface functions
    #--------------------
    def systray_click(self, widget, user_param=None):
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
        self.liststore = gtk.ListStore(gobject.TYPE_STRING,
            gobject.TYPE_INT,
            gobject.TYPE_STRING,
            gobject.TYPE_STRING,
            gobject.TYPE_STRING,
            gobject.TYPE_STRING)

        self.sorter = gtk.TreeModelSort(self.liststore)
        self.sorter.set_sort_column_id(Columns.DATETIME, gtk.SORT_DESCENDING)
        self.treeview = gtk.TreeView(self.sorter)

        self.treeview.set_property('headers-visible', False)
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
        # wrapping: pango.WRAP_WORD = 0, don't need to import pango for that
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

    def open_status_web(self, treeview, path, view_column, user_data=None):
        """ Callback to open status update in web browser when received
        left click.
        """
        model = treeview.get_model()
        if not model:
            return

        iter = model.get_iter(path)
        uid = model.get_value(iter, Columns.UID)
        status_id = model.get_value(iter, Columns.STATUSID)
        status_url = ('http://www.facebook.com/profile.php?' \
            'id=%d&v=feed&story_fbid=%s' % (uid, status_id))
        self.open_url(path, status_url)
        return

    def click_status(self, treeview, event, user_data=None):
        """Callback when a mouse click event occurs on one of the rows."""
        _log.debug('clicked on status list')
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
        _log.debug('show popup menu')
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

        uid = model.get_value(iter, Columns.UID)
        status_id = model.get_value(iter, Columns.STATUSID)
        url = ('http://www.facebook.com/profile.php?' \
            'id=%d&v=feed&story_fbid=%s' % (uid, status_id))
        item_name = 'This status'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        url = ('http://www.facebook.com/profile.php?' \
            'id=%d' % (uid))
        item_name = 'User wall'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        url = ('http://www.facebook.com/profile.php?' \
            'id=%d&v=info' % (uid))
        item_name = 'User info'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        url = ('http://www.facebook.com/profile.php?' \
            'id=%d&v=photos' % (uid))
        item_name = 'User photos'
        item = gtk.MenuItem(item_name)
        item.connect('activate', self.open_url, url)
        open_menu_items.append(item)

        open_menu = gtk.Menu()
        for item in open_menu_items:
            open_menu.append(item)

        # Menu item to open different pages connected to status in browser
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
        comments = int(store.get_value(position, Columns.COMMENTS))
        if comments > 0:
            cell.set_property('text', str(comments))
        else:
            cell.set_property('text', '')

    def _cell_renderer_commentspic(self, column, cell, store, position):
        comments = int(store.get_value(position, Columns.COMMENTS))
        if comments > 0:
            cell.set_property('pixbuf', self.commentspic)
        else:
            cell.set_property('pixbuf', None)

    def _cell_renderer_likes(self, column, cell, store, position):
        likes = int(store.get_value(position, Columns.LIKES))
        if likes > 0:
            cell.set_property('text', str(likes))
        else:
            cell.set_property('text', '')

    def _cell_renderer_likespic(self, column, cell, store, position):
        likes = int(store.get_value(position, Columns.LIKES))
        if likes > 0:
            cell.set_property('pixbuf', self.likespic)
        else:
            cell.set_property('pixbuf', None)

    #------------------
    # Main Window start
    #------------------
    def __init__(self, facebook):
        global spelling_support

        unknown_user = 'pixmaps/unknown_user.png'
        if unknown_user:
            self._default_profilepic = gtk.gdk.pixbuf_new_from_file(
                    unknown_user)
        else:
            self._default_profilepic = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB,
                    has_alpha=False, bits_per_sample=8, width=50, height=50)

        self.commentspic = gtk.gdk.pixbuf_new_from_file('pixmaps/comments.png')
        self.likespic = gtk.gdk.pixbuf_new_from_file('pixmaps/likes.png')

        # create a new window
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_size_request(480, 250)
        self.window.set_title("Minibook")
        self.window.connect("delete_event", lambda w, e: gtk.main_quit())

        vbox = gtk.VBox(False, 0)
        self.window.add(vbox)
        vbox.show()

        self.friendsname = {}
        self.friendsprofilepic = {}
        self._profilepics = {}

        self.create_grid()
        self.statuslist_window = gtk.ScrolledWindow()
        self.statuslist_window.set_policy(gtk.POLICY_NEVER, gtk.POLICY_ALWAYS)
        self.statuslist_window.add(self.treeview)
        self.treeview.show()
        self.statuslist_window.show()
        vbox.add(self.statuslist_window)

        hbox = gtk.HBox(False, 0)

        label = gtk.Label("What's on your mind?")
        hbox.pack_start(label, True, True, 0)
        label.show()
        self.count_label = gtk.Label("(160)")
        hbox.pack_start(self.count_label, True, True, 0)
        self.count_label.show()
        vbox.add(hbox)
        hbox.show()

        self.entry = gtk.TextView()
        text = self.entry.get_buffer()
        text.connect('changed', self.count)
        vbox.pack_start(self.entry, True, True, 0)
        self.entry.show()

        hbox = gtk.HBox(False, 0)
        vbox.add(hbox)
        hbox.show()

        button = gtk.Button(stock=gtk.STOCK_CLOSE)
        button.connect("clicked", lambda w: gtk.main_quit())
        hbox.pack_start(button, True, True, 0)
        button.set_flags(gtk.CAN_DEFAULT)
        button.grab_default()
        button.show()

        button = gtk.Button(stock=gtk.STOCK_ADD)
        button.connect("clicked", lambda w: self.sendupdate())
        hbox.pack_start(button, True, True, 0)
        button.set_flags(gtk.CAN_DEFAULT)
        button.grab_default()
        button.show()

        if spelling_support:
            try:
                spelling = gtkspell.Spell(self.entry, 'en')
            except:
                spelling_support = False

        self.window.show()
        self._facebook = facebook

        self._app_icon = 'pixmaps/minibook.png'
        self._systray = gtk.StatusIcon()
        self._systray.set_from_file(self._app_icon)
        self._systray.set_tooltip('%s\n' \
            'Left-click: toggle window hiding' % (APPNAME))
        self._systray.connect('activate', self.systray_click)
        self._systray.set_visible(True)

        self.window.set_icon_from_file(self._app_icon)

        self._threads = _ThreadManager()

        self.userinfo = self._facebook.users.getInfo([self._facebook.uid], \
            ['name'])[0]
        self._last_update = 0
        self._threads.add_work(self.post_get_friends_list,
                self.except_get_friends_list,
                self.get_friends_list)

        self._prefs = {}
        x, y = self.window.get_position()
        self._prefs['window_pos_x'] = x
        self._prefs['window_pos_y'] = y
        self._prefs['auto_refresh_interval'] = 5

        self._refresh_id = None
        self.set_auto_refresh()


def main(facebook):
    gtk.main()
    gtk.gdk.threads_leave()
    _log.debug('Exiting')
    return 0

if __name__ == "__main__":
    try:
        config_file = open("config", "r")
        api_key = config_file.readline()[:-1]
        secret_key = config_file.readline()[:-1]
        _log.debug('Config file loaded successfully')
    except Exception, e:
        _log.error('Error while loading config file: %s' % (str(e)))
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
    dia = gtk.Dialog('minibook: login',
        None,
        gtk.DIALOG_MODAL | \
        gtk.DIALOG_DESTROY_WITH_PARENT | \
        gtk.DIALOG_NO_SEPARATOR,
        ("Logged in", gtk.RESPONSE_OK, gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL))
    label = gtk.Label("%s is opening your web browser to log in Facebook.\n' \
        When finished, click 'Logged in', or you can cancel now." % (APPNAME))
    dia.vbox.pack_start(label, True, True, 10)
    label.show()
    dia.show()
    result = dia.run()
    if result == gtk.RESPONSE_CANCEL:
        _log.debug('Exiting before Facebook login.')
        exit(0)
    dia.destroy()

    facebook.auth.getSession()
    _log.info('Session Key: %s' % (facebook.session_key))
    _log.info('User\'s UID: %d' % (facebook.uid))

    MainWindow(facebook)
    main(facebook)
