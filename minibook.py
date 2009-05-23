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

LEVELS = {'debug': logging.DEBUG,
          'info': logging.INFO,
          'warning': logging.WARNING,
          'error': logging.ERROR,
          'critical': logging.CRITICAL}

if len(sys.argv) > 1:
    level_name = sys.argv[1]
    level = LEVELS.get(level_name, logging.NOTSET)
    logging.basicConfig(level=level)

_log = logging.getLogger('minibook')


class Columns:
    (STATUSID, UID, STATUS, DATETIME, REPLIES, LIKES) = range(6)


#-------------------------------------------------
# From http://edsiper.linuxchile.cl/blog/?p=152
# to mitigate TreeView + threads problems
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
            self.refresh()

    #------------------------------
    # Information pulling functions
    #------------------------------
    def get_friends_list(self):
        query = ("SELECT uid, name FROM user \
            WHERE (uid IN (SELECT uid2 FROM friend WHERE uid1 = %d) \
            OR uid = %d)" % (self._facebook.uid, self._facebook.uid))
        friends = self._facebook.fql.query([query])
        self.friendsname = {}
        for friend in friends:
            self.friendsname[str(friend['uid'])] = friend['name']

    def post_get_friends_list(self, widget, results):
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

        _log.info("Fetch every status published since %s" \
            % (time.strftime("%c", time.localtime(since))))

        query = ('SELECT uid, time, status_id, message FROM status \
            WHERE (uid IN (SELECT uid2 FROM friend WHERE uid1 = %d) \
            OR uid = %d) \
            AND time  > %d \
            ORDER BY time DESC\
            LIMIT 60' \
            % (self._facebook.uid, self._facebook.uid, since))
        _log.debug('Status list query: %s' % (query))

        status = self._facebook.fql.query([query])

        _log.info('Received %d new status' % (len(status)))

        for up in status:
            self.liststore.append((up['status_id'],
                up['uid'],
                up['message'],
                up['time'],
                '0',
                '0'))

    def post_get_status_list(self, widget, results):
        _log.debug("Status updates successfully pulled.")
        self._last_update = int(time.time())
        return

    def except_get_status_list(self, widget, exception):
        _log.error("Get status list exception: %s" % (str(exception)))

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
        datetime = time.localtime(float(store.get_value(position, \
            Columns.DATETIME)))
        displaytime = time.strftime('%c', datetime)

        #replace characters that would choke the markup
        status = re.sub(r'&', r'&amp;', status)
        status = re.sub(r'<', r'&lt;', status)
        status = re.sub(r'>', r'&gt;', status)
        markup = ('<b>%s</b> %s\non %s' % \
                (name, status, displaytime))
        _log.debug('Marked up text: %s' % (markup))
        cell.set_property('markup', markup)
        return

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
        self.treeview = gtk.TreeView(self.liststore)
        self.treeview.set_property('headers-visible', False)
        self.treeview.set_rules_hint(True)

        self.status_renderer = gtk.CellRendererText()
        #~ self.status_renderer.set_property('wrap-mode', gtk.WRAP_WORD)
        self.status_renderer.set_property('wrap-width', 350)
        self.status_renderer.set_property('width', 10)

        self.status_column = gtk.TreeViewColumn('Message', \
                self.status_renderer, text=1)
        self.status_column.set_cell_data_func(self.status_renderer, \
                self.status_format)
        self.treeview.append_column(self.status_column)
        self.treeview.set_resize_mode(gtk.RESIZE_IMMEDIATE)

    #------------------
    # Main Window start
    #------------------
    def __init__(self, facebook):
        global spelling_support

        # create a new window
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_size_request(400, 250)
        self.window.set_title("Minibook")
        self.window.connect("delete_event", lambda w, e: gtk.main_quit())

        vbox = gtk.VBox(False, 0)
        self.window.add(vbox)
        vbox.show()

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

        self._app_icon = 'minibook.png'
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
    facebook.auth.createToken()
    facebook.login()
    _log.debug('Showing Facebook login page in default browser.')

    # Delay dialog to allow for login in browser
    dia = gtk.Dialog('minibook: login',
        None,
        gtk.DIALOG_MODAL | \
        gtk.DIALOG_DESTROY_WITH_PARENT | \
        gtk.DIALOG_NO_SEPARATOR,
        ("Logged In", gtk.RESPONSE_OK, gtk.STOCK_CANCEL, gtk.RESPONSE_CLOSE))
    label = gtk.Label("Click after logging in to Facebook in your browser:")
    dia.vbox.pack_start(label, True, True, 10)
    label.show()
    dia.show()
    result = dia.run()
    if result == gtk.RESPONSE_CLOSE:
        _log.debug('Exiting before Facebook login.')
        exit(0)
    dia.destroy()

    facebook.auth.getSession()
    _log.info('Session Key: %s' % (facebook.session_key))
    _log.info('User\'s UID: %d' % (facebook.uid))

    MainWindow(facebook)
    main(facebook)
