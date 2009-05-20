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
from facebook import Facebook

try:
    import gtkspell
    spelling_support = True
except:
    spelling_support = False


class Columns:
    (ID, STATUS, DATETIME, REPLIES, LIKES) = range(5)


class MainWindow:
    """The main application interface"""

    def enter_callback(self, widget, entry):
        entry_text = entry.get_buffer().get_text()
        print "Entry contents: %s\n" % entry_text

    def sendupdate(self):
        textfield = self.entry.get_buffer()
        start = textfield.get_start_iter()
        end = textfield.get_end_iter()
        entry_text = textfield.get_text(start, end)
        if entry_text != "":
            print "Sent entry contents: %s\n" % entry_text
            self._facebook.status.set([entry_text], [self._facebook.uid])

            textfield.set_text("")

    def getupdates(self):
        list = self._facebook.status.get([self._facebook.uid], [10])
        status_list = []
        for status in list:
            print status
            status_list.append((status['status_id'],
                status['message'],
                status['time'],
                '0',
                '0'))
        for data in status_list:
            self.liststore.append(data)

    def count(self, text):
        start = text.get_start_iter()
        end = text.get_end_iter()
        thetext = text.get_text(start, end)
        self.count_label.set_text('(%d)' % (160 - len(thetext)))
        return True

    def __init__(self, facebook):
        global spelling_support

        # create a new window
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_size_request(400, 100)
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

        self.userinfo = self._facebook.users.getInfo([self._facebook.uid], \
            ['name'])[0]
        self.getupdates()

    def systray_click(self, widget, user_param=None):
        if self.window.get_property('visible'):
            self.window.hide()
        else:
            self.window.deiconify()
            self.window.present()

    def create_grid(self):
        self.liststore = gtk.ListStore(gobject.TYPE_STRING,
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

    def status_format(self, column, cell, store, position):
        status = store.get_value(position, Columns.STATUS)
        name = self.userinfo['name']
        markup = '%s %s' % \
                (name, status)
        cell.set_property('markup', markup)
        return


def main(facebook):
    gtk.main()
    return 0

if __name__ == "__main__":
    try:
        config_file = open("config", "r")
        api_key = config_file.readline()[:-1]
        secret_key = config_file.readline()[:-1]
    except Exception, e:
        exit('Error while loading config file: %s' % (str(e)))
    facebook = Facebook(api_key, secret_key)
    facebook.auth.createToken()
    facebook.login()
    facebook.auth.getSession()
    print 'Session Key:   ', facebook.session_key
    print 'Your UID:      ', facebook.uid

    MainWindow(facebook)
    main(facebook)
