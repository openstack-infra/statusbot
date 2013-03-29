#! /usr/bin/env python

# Copyright 2011, 2013 OpenStack Foundation
# Copyright 2012 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The configuration file should look like:
"""
[ircbot]
nick=NICKNAME
pass=PASSWORD
server=irc.freenode.net
port=6667
channels=foo,bar
nicks=alice,bob

[wiki]
user=StatusBot
password=password
url=https://wiki.example.com/w/api.php
pageid=1781
"""

import argparse
import ConfigParser
import daemon
import irc.bot
import logging.config
import os
import threading
import time
import simplemediawiki
import datetime
import re

try:
    import daemon.pidlockfile
    pid_file_module = daemon.pidlockfile
except:
    # as of python-daemon 1.6 it doesn't bundle pidlockfile anymore
    # instead it depends on lockfile-0.9.1
    import daemon.pidfile
    pid_file_module = daemon.pidfile


class UpdateInterface(object):
    def alert(self, msg=None):
        raise NotImplementedError()

    def notice(self, msg=None):
        raise NotImplementedError()

    def log(self, msg=None):
        raise NotImplementedError()

    def ok(self, msg=None):
        raise NotImplementedError()


class StatusPage(UpdateInterface):
    alert_re = re.compile(r'{{CI Alert\|(.*?)}}')
    item_re = re.compile(r'^\* (.*)$')

    def __init__(self, config):
        self.url = config.get('wiki', 'url')
        self.pageid = config.get('wiki', 'pageid')
        self.username = config.get('wiki', 'username')
        self.password = config.get('wiki', 'password')
        self.current_alert = None
        self.items = []

    def alert(self, msg):
        self.update(set_alert=True, msg=msg)

    def notice(self, msg):
        self.update(msg=msg)

    def log(self, msg):
        self.update(msg=msg)

    def ok(self, msg):
        self.update(clear_alert=True, msg=msg)

    def update(self, set_alert=None, clear_alert=None, msg=None):
        self.wiki = simplemediawiki.MediaWiki(self.url)
        self.wiki.login(self.username, self.password)
        self.load()
        if set_alert:
            self.setAlert(msg)
        if clear_alert:
            self.setAlert(None)
        if msg:
            self.addItem(msg)
        self.save()

    def load(self):
        self.current_alert = None
        self.items = []
        data = self.wiki.call(dict(action='query',
                                   prop='revisions',
                                   rvprop='content',
                                   pageids=self.pageid,
                                   format='json'))
        text = data['query']['pages'][str(self.pageid)]['revisions'][0]['*']
        for line in text.split('\n'):
            m = self.alert_re.match(line)
            if m:
                self.current_alert = m.group(1)
            m = self.item_re.match(line)
            if m:
                self.items.append(m.group(1))

    def save(self):
        text = ''
        if self.current_alert:
            text += '{{CI Alert|%s}}\n\n' % self.current_alert
        for item in self.items:
            text += '* %s\n' % item

        data = self.wiki.call(dict(action='query',
                                   prop='info',
                                   pageids=self.pageid,
                                   intoken='edit'))
        token = data['query']['pages'][str(self.pageid)]['edittoken']
        data = self.wiki.call(dict(action='edit',
                                   pageid=self.pageid,
                                   bot=True,
                                   text=text,
                                   token=token))

    def addItem(self, item, ts=None):
        if not ts:
            ts = datetime.datetime.now()
        text = '%s %s' % (ts.strftime("%Y-%m-%d %H:%M:%S UTC"), item)
        self.items.insert(0, text)

    def setAlert(self, current_alert):
        self.current_alert = current_alert


class StatusBot(irc.bot.SingleServerIRCBot):
    log = logging.getLogger("statusbot.bot")

    def __init__(self, channels, nicks, publishers,
                 nickname, password, server, port=6667):
        irc.bot.SingleServerIRCBot.__init__(self,
                                           [(server, port)],
                                           nickname, nickname)
        self.channel_list = channels
        self.nicks = nicks
        self.nickname = nickname
        self.password = password
        self.identify_msg_cap = False
        self.ignore_topics = True
        self.topic_lock = threading.Lock()
        self.topics = {}
        self.publishers = publishers

    def on_nicknameinuse(self, c, e):
        self.log.debug("Nickname in use, releasing")
        c.nick(c.get_nickname() + "_")
        c.privmsg("nickserv", "identify %s " % self.password)
        c.privmsg("nickserv", "ghost %s %s" % (self.nickname, self.password))
        c.privmsg("nickserv", "release %s %s" % (self.nickname, self.password))
        time.sleep(1)
        c.nick(self.nickname)

    def on_welcome(self, c, e):
        self.identify_msg_cap = False
        self.log.debug("Requesting identify-msg capability")
        c.cap('REQ', 'identify-msg')
        c.cap('END')
        self.log.debug("Identifying to nickserv")
        c.privmsg("nickserv", "identify %s " % self.password)
        for channel in self.channel_list:
            self.log.info("Joining %s" % channel)
            c.join(channel)

    def on_cap(self, c, e):
        self.log.debug("Received cap response %s" % repr(e.arguments))
        if e.arguments[0] == 'ACK' and 'identify-msg' in e.arguments[1]:
            self.log.debug("identify-msg cap acked")
            self.identify_msg_cap = True

    def on_pubmsg(self, c, e):
        if not self.identify_msg_cap:
            self.log.debug("Ignoring message because identify-msg "
                           "cap not enabled")
            return
        nick = e.source.split('!')[0]
        auth = e.arguments[0][0]
        msg = e.arguments[0][1:]
        if not msg.startswith('#status'):
            return
        if auth != '+':
            self.log.debug("Ignoring message from unauthenticated "
                           "user %s" % nick)
            return
        if nick not in self.nicks:
            self.log.debug("Ignoring message from untrusted user %s" % nick)
            return
        try:
            self.handle_command(nick, msg)
        except:
            self.log.exception("Exception handling command %s" % msg)

    def handle_command(self, nick, msg):
        parts = msg.split()
        command = parts[1].lower()
        text = ' '.join(parts[2:])

        if command == 'alert':
            self.log.info("Processing alert from %s: %s" % (nick, text))
            self.set_all_topics(text)
            self.broadcast('NOTICE: ' + text)
            for p in self.publishers:
                p.alert(text)
        elif command == 'notice':
            self.log.info("Processing notice from %s: %s" % (nick, text))
            self.broadcast('NOTICE: ' + text)
            for p in self.publishers:
                p.notice(text)
        elif command == 'log':
            self.log.info("Processing log from %s: %s" % (nick, text))
            for p in self.publishers:
                p.log(text)
        elif command == 'ok':
            self.log.info("Processing ok from %s: %s" % (nick, text))
            self.restore_all_topics()
            if text:
                self.broadcast('NOTICE: ' + text)
            for p in self.publishers:
                p.ok(text)
        else:
            self.log.info("Unknown command %s from %s: %s" % (
                    command, nick, msg))

    def broadcast(self, msg):
        for channel in self.channel_list:
            self.send(channel, msg)

    def restore_all_topics(self):
        t = threading.Thread(target=self._restore_all_topics, args=())
        t.start()

    def _restore_all_topics(self):
        self.topic_lock.acquire()
        try:
            if self.topics:
                for channel in self.channel_list:
                    self.set_topic(channel, self.topics[channel])
                self.topics = {}
        finally:
            self.topic_lock.release()

    def set_all_topics(self, topic):
        t = threading.Thread(target=self._set_all_topics, args=(topic,))
        t.start()

    def _set_all_topics(self, topic):
        self.topic_lock.acquire()
        try:
            if not self.topics:
                self.save_topics()
            for channel in self.channel_list:
                self.set_topic(channel, topic)
        finally:
            self.topic_lock.release()

    def save_topics(self):
        # Save all the current topics
        self.ignore_topics = False
        for channel in self.channel_list:
            self.connection.topic(channel)
            time.sleep(0.5)
        start = time.time()
        done = False
        while time.time() < start + 300:
            if len(self.topics) == len(self.channel_list):
                done = True
                break
            time.sleep(0.5)
        self.ignore_topics = True
        if not done:
            raise Exception("Unable to save topics")

    def on_currenttopic(self, c, e):
        if self.ignore_topics:
            return
        self.topics[e.arguments[0]] = e.arguments[1]

    def send(self, channel, msg):
        self.connection.privmsg(channel, msg)
        time.sleep(0.5)

    def set_topic(self, channel, topic):
        self.connection.topic(channel, topic)
        self.connection.privmsg('ChanServ', 'topic %s %s' % (channel, topic))
        time.sleep(0.5)


def _main(configpath):
    config = ConfigParser.ConfigParser()
    config.read(configpath)
    setup_logging(config)

    channels = ['#' + name.strip() for name in
                config.get('ircbot', 'channels').split(',')]
    nicks = [name.strip() for name in
             config.get('ircbot', 'nicks').split(',')]
    publishers = [StatusPage(config)]

    bot = StatusBot(channels, nicks, publishers,
                    config.get('ircbot', 'nick'),
                    config.get('ircbot', 'pass'),
                    config.get('ircbot', 'server'),
                    config.getint('ircbot', 'port'))
    bot.start()


def main():
    parser = argparse.ArgumentParser(description='Status bot.')
    parser.add_argument('-c', dest='config', nargs=1,
                        help='specify the config file')
    parser.add_argument('-d', dest='nodaemon', action='store_true',
                        help='do not run as a daemon')
    args = parser.parse_args()

    if not args.nodaemon:
        pid = pid_file_module.TimeoutPIDLockFile(
            "/var/run/statusbot/statusbot.pid", 10)
        with daemon.DaemonContext(pidfile=pid):
            _main(args.config)
    _main(args.config)


def setup_logging(config):
    if config.has_option('ircbot', 'log_config'):
        log_config = config.get('ircbot', 'log_config')
        fp = os.path.expanduser(log_config)
        if not os.path.exists(fp):
            raise Exception("Unable to read logging config file at %s" % fp)
        logging.config.fileConfig(fp)
    else:
        logging.basicConfig(level=logging.DEBUG)


if __name__ == "__main__":
    main()
