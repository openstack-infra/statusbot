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
username=StatusBot
password=password
url=https://wiki.example.com/w/api.php
pageid=1781
successpageid=2434
thankspageid=37700

[irclogs]
url=http://eavesdrop.example.com/irclogs/%(chan)s/%(chan)s.%(date)s.log.html

[twitter]
consumer_key=consumer_key
consumer_secret=consumer_secret
access_token_key=access_token_key
access_token_secret=access_token_secret
"""

import argparse
import ConfigParser
import daemon
import irc.bot
import json
import logging.config
import os
import tempfile
import time
import simplemediawiki
import datetime
import re
import ssl
import textwrap
import twitter
import urllib

try:
    import daemon.pidlockfile as pid_file_module
except ImportError:
    # as of python-daemon 1.6 it doesn't bundle pidlockfile anymore
    # instead it depends on lockfile-0.9.1
    import daemon.pidfile as pid_file_module


# https://bitbucket.org/jaraco/irc/issue/34/
# irc-client-should-not-crash-on-failed
# ^ This is why pep8 is a bad idea.
irc.client.ServerConnection.buffer_class.errors = 'replace'
ANTI_FLOOD_SLEEP = 2


class WikiPage(object):
    def __init__(self, config):
        self.url = config.get('wiki', 'url')
        self.pageid = config.get('wiki', 'pageid')
        self.username = config.get('wiki', 'username')
        self.password = config.get('wiki', 'password')

    def login(self):
        self.wiki = simplemediawiki.MediaWiki(self.url)
        self.wiki.login(self.username, self.password)

    def load(self):
        data = self.wiki.call(dict(action='query',
                                   prop='revisions',
                                   rvprop='content',
                                   pageids=self.pageid,
                                   format='json'))
        return data['query']['pages'][str(self.pageid)]['revisions'][0]['*']

    def timestamp(self, ts=None):
        if not ts:
            ts = datetime.datetime.now()
        return ts.strftime("%Y-%m-%d %H:%M:%S UTC")

    def save(self, text):
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


class SuccessPage(WikiPage):
    def __init__(self, config):
        super(SuccessPage, self).__init__(config)
        if config.has_option('wiki', 'successpageid'):
            self.pageid = config.get('wiki', 'successpageid')
        else:
            self.pageid = None
        if config.has_option('irclogs', 'url'):
            self.irclogs_url = config.get('irclogs', 'url')
        else:
            self.irclogs_url = None

    def log(self, channel, nick, msg):
        if self.pageid:
            self.login()
            ts = self.timestamp()
            if self.irclogs_url:
                url = self.irclogs_url % {
                    'chan': urllib.quote(channel),
                    'date': ts[0:10]}
                onchan = "[%s %s]" % (url, channel)
            else:
                onchan = channel
            data = self.load()
            current = data.split("\n")
            newtext = "%s\n|-\n| %s || %s (on %s) || %s\n%s" % (
                current[0], ts, nick, onchan, msg, '\n'.join(current[1:]))
            self.save(newtext)


class ThanksPage(WikiPage):
    def __init__(self, config):
        super(ThanksPage, self).__init__(config)
        if config.has_option('wiki', 'thankspageid'):
            self.pageid = config.get('wiki', 'thankspageid')
        else:
            self.pageid = None
        if config.has_option('irclogs', 'url'):
            self.irclogs_url = config.get('irclogs', 'url')
        else:
            self.irclogs_url = None

    def log(self, channel, nick, msg):
        if self.pageid:
            self.login()
            ts = self.timestamp()
            if self.irclogs_url:
                url = self.irclogs_url % {
                    'chan': urllib.quote(channel),
                    'date': ts[0:10]}
                onchan = "[%s %s]" % (url, channel)
            else:
                onchan = channel
            data = self.load()
            current = data.split("\n")
            newtext = "%s\n|-\n| %s || %s (on %s) || %s\n%s" % (
                current[0], ts, nick, onchan, msg, '\n'.join(current[1:]))
            self.save(newtext)


class UpdateInterface(object):
    def alert(self, msg=None):
        pass

    def notice(self, msg=None):
        pass

    def log(self, msg=None):
        pass

    def ok(self, msg=None):
        pass


class Tweet(UpdateInterface):

    def __init__(self, config):
        self.consumer_key = config.get('twitter', 'consumer_key')
        self.consumer_secret = config.get('twitter', 'consumer_secret')
        self.access_token_key = config.get('twitter', 'access_token_key')
        self.access_token_secret = config.get('twitter', 'access_token_secret')
        self.api = twitter.Api(
            consumer_key=self.consumer_key,
            consumer_secret=self.consumer_secret,
            access_token_key=self.access_token_key,
            access_token_secret=self.access_token_secret)

    def update(self, msg):
        # Limit tweets to 120 characters to facilitate retweets
        tweets = textwrap.wrap(msg, 120)
        if len(tweets) == 1:
            # Don't prefix statuses that fit
            self.api.PostUpdate(tweets[0])
        else:
            for index in range(0, len(tweets)):
                self.api.PostUpdate("{index}/{tweet}".format(
                    index=index, tweet=tweets[index]))

    def alert(self, msg=None):
        self.update(msg)

    def notice(self, msg=None):
        self.update(msg)

    def log(self, msg=None):
        pass

    def ok(self, msg=None):
        if msg:
            self.update(msg)
        else:
            self.update("Everything back to normal")


class StatusPage(WikiPage, UpdateInterface):
    alert_re = re.compile(r'{{CI Alert\|(.*?)}}')
    item_re = re.compile(r'^\* (.*)$')

    def __init__(self, config):
        super(StatusPage, self).__init__(config)
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
        self.login()
        self.loadItems()
        if set_alert:
            self.setAlert(msg)
        if clear_alert:
            self.setAlert(None)
        if msg:
            self.addItem(msg)
        self.saveItems()

    def loadItems(self):
        self.current_alert = None
        self.items = []
        text = self.load()
        for line in text.split('\n'):
            m = self.alert_re.match(line)
            if m:
                self.current_alert = m.group(1)
            m = self.item_re.match(line)
            if m:
                self.items.append(m.group(1))

    def saveItems(self):
        text = ''
        if self.current_alert:
            text += '{{CI Alert|%s}}\n\n' % self.current_alert
        for item in self.items:
            text += '* %s\n' % item
        self.save(text)

    def addItem(self, item, ts=None):
        text = '%s %s' % (self.timestamp(ts=ts), item)
        self.items.insert(0, text)

    def setAlert(self, current_alert):
        self.current_alert = current_alert


class AlertFile(UpdateInterface):
    def __init__(self, config):
        if config.has_section('alertfile'):
            self.dir = config.get('alertfile', 'dir')
            self.path = os.path.join(self.dir, 'alert.json')
        else:
            self.path = None
        self.ok()

    def write(self, msg):
        if not self.path:
            return
        f, path = tempfile.mkstemp(dir=self.dir)
        os.write(f, json.dumps(dict(alert=msg)))
        os.close(f)
        os.chmod(path, 0o644)
        os.rename(path, self.path)

    def alert(self, msg=None):
        self.write(msg)

    def ok(self, msg=None):
        self.write(None)


class StatusBot(irc.bot.SingleServerIRCBot):
    log = logging.getLogger("statusbot.bot")

    def __init__(self, channels, nicks, publishers, successlog, thankslog,
                 nickname, password, server, port=6667):
        if port == 6697:
            factory = irc.connection.Factory(wrapper=ssl.wrap_socket)
            irc.bot.SingleServerIRCBot.__init__(self,
                                                [(server, port)],
                                                nickname, nickname,
                                                connect_factory=factory)
        else:
            irc.bot.SingleServerIRCBot.__init__(self,
                                                [(server, port)],
                                                nickname, nickname)
        self.channel_list = channels
        self.nicks = nicks
        self.nickname = nickname
        self.password = password
        self.identify_msg_cap = False
        self.topics = {}
        self.current_topic = None
        self.publishers = publishers
        self.successlog = successlog
        self.thankslog = thankslog

    def on_nicknameinuse(self, c, e):
        self.log.debug("Nickname in use, releasing")
        c.nick(c.get_nickname() + "_")
        c.privmsg("nickserv", "identify %s " % self.password)
        c.privmsg("nickserv", "ghost %s %s" % (self.nickname, self.password))
        c.privmsg("nickserv", "release %s %s" % (self.nickname, self.password))
        time.sleep(ANTI_FLOOD_SLEEP)
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
            time.sleep(ANTI_FLOOD_SLEEP)

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
        # Unprivileged commands
        if msg.startswith('#success'):
            self.handle_success_command(e.target, nick, msg)
            return
        if msg.startswith('#thanks'):
            self.handle_thanks_command(e.target, nick, msg)
            return
        # Privileged commands
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
            self.handle_status_command(e.target, nick, msg)
        except Exception:
            self.log.exception("Exception handling command %s" % msg)

    def handle_success_command(self, channel, nick, msg):
        parts = msg.split()
        text = ' '.join(parts[1:])
        self.log.info("Processing success from %s: %s" % (nick, text))
        self.successlog.log(channel, nick, text)
        self.send(channel, "%s: Added success to Success page" % (nick,))

    def handle_thanks_command(self, channel, nick, msg):
        parts = msg.split()
        text = ' '.join(parts[1:])
        self.log.info("Processing thanks from %s: %s" % (nick, text))
        self.thankslog.log(channel, nick, text)
        self.send(channel, "%s: Added thanks to Thanks page" % (nick,))

    def handle_status_command(self, channel, nick, msg):
        parts = msg.split()
        command = parts[1].lower()
        text = ' '.join(parts[2:])

        if command == 'alert':
            self.log.info("Processing alert from %s: %s" % (nick, text))
            self.send(channel, "%s: sending alert" % (nick,))
            self.broadcast('NOTICE: ', text, set_topic=True)
            for p in self.publishers:
                p.alert(text)
            self.send(channel, "%s: finished sending alert" % (nick,))
        elif command == 'notice':
            self.log.info("Processing notice from %s: %s" % (nick, text))
            self.send(channel, "%s: sending notice" % (nick,))
            self.broadcast('NOTICE: ', text)
            for p in self.publishers:
                p.notice(text)
            self.send(channel, "%s: finished sending notice" % (nick,))
        elif command == 'log':
            self.log.info("Processing log from %s: %s" % (nick, text))
            for p in self.publishers:
                p.log(text)
            self.send(channel, "%s: finished logging" % (nick,))
        elif command == 'ok':
            self.log.info("Processing ok from %s: %s" % (nick, text))
            self.send(channel, "%s: sending ok" % (nick,))
            self.broadcast('NOTICE: ', text, restore_topic=True)
            for p in self.publishers:
                p.ok(text)
            self.send(channel, "%s: finished sending ok" % (nick,))
        else:
            self.send(channel, "%s: unknown command" % (nick,))
            self.log.info(
                "Unknown command %s from %s: %s" % (command, nick, msg))

    def broadcast(self, prefix, msg, set_topic=False, restore_topic=False):
        if set_topic:
            self.current_topic = msg
        for channel in self.channel_list:
            if restore_topic:
                # Set to the saved topic or just the channel name if
                # we don't have a saved topic (to avoid leaving it as
                # the alert).
                t = self.topics.get(channel, channel)
                self.set_topic(channel, t)
            if msg:
                self.notice(channel, prefix + msg)
            if set_topic:
                self.set_topic(channel, msg)

    def on_currenttopic(self, c, e):
        channel, topic = (e.arguments[0], e.arguments[1])
        self.update_saved_topic(channel, topic)

    def on_topic(self, c, e):
        channel, topic = (e.target, e.arguments[0])
        self.update_saved_topic(channel, topic)

    def update_saved_topic(self, channel, topic):
        if topic == self.current_topic:
            return
        self.log.info("Current topic on %s is %s" % (channel, topic))
        self.topics[channel] = topic

    def notice(self, channel, msg):
        self.connection.notice(channel, msg)
        time.sleep(ANTI_FLOOD_SLEEP)

    def send(self, channel, msg):
        self.connection.privmsg(channel, msg)
        time.sleep(ANTI_FLOOD_SLEEP)

    def set_topic(self, channel, topic):
        self.log.info("Setting topic on %s to %s" % (channel, topic))
        self.connection.privmsg('ChanServ', 'topic %s %s' % (channel, topic))
        time.sleep(ANTI_FLOOD_SLEEP)


def _main(configpath):
    config = ConfigParser.RawConfigParser()
    config.read(configpath)
    setup_logging(config)

    channels = ['#' + name.strip() for name in
                config.get('ircbot', 'channels').split(',')]
    nicks = [name.strip() for name in
             config.get('ircbot', 'nicks').split(',')]
    publishers = [StatusPage(config),
                  AlertFile(config)]
    successlog = SuccessPage(config)
    thankslog = ThanksPage(config)
    if config.has_section('twitter'):
        publishers.append(Tweet(config))

    bot = StatusBot(channels, nicks, publishers, successlog, thankslog,
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
