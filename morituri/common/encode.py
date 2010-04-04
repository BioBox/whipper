# -*- Mode: Python; test-case-name: morituri.test.test_common_encode -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of morituri.
# 
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.

import math

from morituri.common import common, task

from morituri.common import log
log.init()

class Profile(object):
    name = None
    extension = None
    pipeline = None
    losless = None

    def test(self):
        """
        Test if this profile will work.
        Can check for elements, ...
        """
        pass

class FlacProfile(Profile):
    name = 'flac'
    extension = 'flac'
    pipeline = 'flacenc name=tagger quality=8'
    lossless = True

    # FIXME: we should do something better than just printing ERRORS
    def test(self):

        # here to avoid import gst eating our options
        import gst

        plugin = gst.registry_get_default().find_plugin('flac')
        if not plugin:
            print 'ERROR: cannot find flac plugin'
            return False

        versionTuple = tuple([int(x) for x in plugin.get_version().split('.')])
        if len(versionTuple) < 4:
            versionTuple = versionTuple + (0, )
        if versionTuple > (0, 10, 9, 0) and versionTuple <= (0, 10, 15, 0):
            print 'ERROR: flacenc between 0.10.9 and 0.10.15 has a bug'
            return False

        return True

class AlacProfile(Profile):
    name = 'alac'
    extension = 'alac'
    pipeline = 'ffenc_alac name=tagger'
    lossless = True

class WavProfile(Profile):
    name = 'wav'
    extension = 'wav'
    pipeline = 'wavenc name=tagger'
    lossless = True

class WavpackProfile(Profile):
    name = 'wavpack'
    extension = 'wv'
    pipeline = 'wavpackenc bitrate=0 name=tagger'
    lossless = True

class MP3Profile(Profile):
    name = 'mp3'
    extension = 'mp3'
    pipeline = 'lame name=tagger quality=0 ! id3v2mux'
    lossless = False

class VorbisProfile(Profile):
    name = 'vorbis'
    extension = 'oga'
    pipeline = 'audioconvert ! vorbisenc name=tagger ! oggmux'
    lossless = False


PROFILES = {
    'wav':     WavProfile,
    'flac':    FlacProfile,
    'alac':    AlacProfile,
    'wavpack': WavpackProfile,
}

LOSSY_PROFILES = {
    'mp3':     MP3Profile,
    'vorbis':  VorbisProfile,
}

ALL_PROFILES = PROFILES.copy()
ALL_PROFILES.update(LOSSY_PROFILES)

class EncodeTask(task.Task):
    """
    I am a task that encodes a .wav file.
    I set tags too.
    I also calculate the peak level of the track.

    @param peak: the peak volume, from 0.0 to 1.0.  This is the sqrt of the
                 peak power.
    @type  peak: float
    """

    description = 'Encoding'
    peak = None

    def __init__(self, inpath, outpath, profile, taglist=None):
        """
        @param profile: encoding profile
        @type  profile: L{Profile}
        """
        assert type(inpath) is unicode, "inpath %r is not unicode" % inpath
        assert type(outpath) is unicode, \
            "outpath %r is not unicode" % outpath
        
        self._inpath = inpath
        self._outpath = outpath
        self._taglist = taglist

        self._level = None
        self._peakdB = None
        self._profile = profile

        self._profile.test()

    def start(self, runner):
        task.Task.start(self, runner)

        # here to avoid import gst eating our options
        import gst

        self._pipeline = gst.parse_launch('''
            filesrc location="%s" !
            decodebin name=decoder !
            audio/x-raw-int,width=16,depth=16,channels=2 !
            level name=level !
            %s !
            filesink location="%s" name=sink''' % (
                self._inpath.encode('utf-8'),
                self._profile.pipeline,
                self._outpath.encode('utf-8')))

        tagger = self._pipeline.get_by_name('tagger')

        # set tags
        if self._taglist:
            tagger.merge_tags(self._taglist, gst.TAG_MERGE_APPEND)

        self.debug('pausing pipeline')
        self._pipeline.set_state(gst.STATE_PAUSED)
        self._pipeline.get_state()
        self.debug('paused pipeline')

        # get length
        self.debug('query duration')
        length, qformat = tagger.query_duration(gst.FORMAT_DEFAULT)
        # wavparse 0.10.14 returns in bytes
        if qformat == gst.FORMAT_BYTES:
            self.debug('query returned in BYTES format')
            length /= 4
        self.debug('total length: %r', length)
        self._length = length

        # add eos handling
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::eos', self._message_eos_cb)

        # set up level callbacks
        bus.connect('message::element', self._message_element_cb)
        self._level = self._pipeline.get_by_name('level')
        # add a probe so we can track progress
        # we connect to level because this gives us offset in samples
        srcpad = self._level.get_static_pad('src')
        srcpad.add_buffer_probe(self._probe_handler)

        self.debug('scheduling setting to play')
        # since set_state returns non-False, adding it as timeout_add
        # will repeatedly call it, and block the main loop; so
        #   gobject.timeout_add(0L, self._pipeline.set_state, gst.STATE_PLAYING)
        # would not work.

        def play():
            self._pipeline.set_state(gst.STATE_PLAYING)
            return False
        self.runner.schedule(0, play)

        #self._pipeline.set_state(gst.STATE_PLAYING)
        self.debug('scheduled setting to play')

    def _probe_handler(self, pad, buffer):
        # update progress based on buffer offset (expected to be in samples)
        # versus length in samples
        # marshal to main thread
        self.runner.schedule(0, self.setProgress,
            float(buffer.offset) / self._length)

        # don't drop the buffer
        return True

    def _message_eos_cb(self, bus, message):
        self.debug('eos, scheduling stop')
        self.runner.schedule(0, self.stop)

    def _message_element_cb(self, bus, message):
        if message.src != self._level:
            return

        s = message.structure
        if s.get_name() != 'level':
            return


        if self._peakdB is None:
            self._peakdB = s['peak'][0]

        for p in s['peak']:
            if self._peakdB < p:
                self._peakdB = p

    def stop(self):
        # here to avoid import gst eating our options
        import gst

        self.debug('stopping')
        self.debug('setting state to NULL')
        self._pipeline.set_state(gst.STATE_NULL)
        self.debug('set state to NULL')
        task.Task.stop(self)

        self.peak = math.sqrt(math.pow(10, self._peakdB / 10.0))
