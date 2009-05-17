#!/usr/bin/env python
""" Minibook: the Facebook(TM) status updater
(C) 2009 Gergely Imreh <imrehg@gmail.com>
"""

import pygtk
pygtk.require('2.0')
import gtk
from facebook import Facebook

class MainWindow:
    def enter_callback(self, widget, entry):
        entry_text = entry.get_buffer().get_text()
        print "Entry contents: %s\n" % entry_text
        
    def sendupdate(self):
        textfield = self.entry.get_buffer()
        start = textfield.get_start_iter()
        end = textfield.get_end_iter()
        entry_text = textfield.get_text(start, end)
        if entry_text != "" :
            print "Sent entry contents: %s\n" % entry_text
            self._facebook.status.set([entry_text],[self._facebook.uid])

            textfield.set_text("")

    def __init__(self,facebook):
        # create a new window
        window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        window.set_size_request(400, 100)
        window.set_title("Minibook")
        window.connect("delete_event", lambda w,e: gtk.main_quit())

        vbox = gtk.VBox(False, 0)
        window.add(vbox)
        vbox.show()
        
        label = gtk.Label("What's on your mind?")
        vbox.pack_start(label, True, True, 0)
        label.show()
        
        self.entry = gtk.TextView()
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
        
        window.show()
        self._facebook = facebook
        
        self._app_icon = 'minibook.png'
        self._systray = gtk.StatusIcon()
        self._systray.set_from_file(self._app_icon)
        self._systray.set_visible(True)

def main(facebook):
    facebook.auth.createToken()
    facebook.login()

    facebook.auth.getSession()
    print 'Session Key:   ', facebook.session_key
    print 'Your UID:      ', facebook.uid
    
    gtk.main()
    return 0

if __name__ == "__main__":
    config_file = open("config", "r")
    api_key = config_file.readline()[:-1]
    secret_key = config_file.readline()[:-1]
    facebook = Facebook(api_key,secret_key)
    MainWindow(facebook)
    main(facebook)
