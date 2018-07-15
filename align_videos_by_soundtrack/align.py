#! /usr/bin/env python
# -*- coding: utf-8 -*-
#
# This script based on alignment_by_row_channels.py by Allison Deal, see
# https://github.com/allisonnicoledeal/VideoSync/blob/master/alignment_by_row_channels.py
"""
This module contains the detector class for knowing the offset
difference for audio and video files, containing audio recordings
from the same event. It relies on ffmpeg being installed and
the python libraries scipy and numpy.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

import os
import sys
from collections import defaultdict
import math
import json
import tempfile
import shutil
import logging

import numpy as np

from . import communicate
from .utils import check_and_decode_filenames
from . import _cache


__all__ = [
    'SyncDetector',
    'main',
    ]

_logger = logging.getLogger(__name__)


def _mk_freq_trans_summary(data, fft_bin_size, overlap, box_height, box_width, maxes_per_box):
    """
    Return characteristic frequency transition's summary.

    The dictionaries to be returned are as follows:
    * key: The frequency appearing as a peak in any time zone.
    * value: A list of the times at which specific frequencies occurred.
    """
    freqs_dict = defaultdict(list)

    boxes = defaultdict(list)
    for x, j in enumerate(range(int(-overlap), len(data), int(fft_bin_size - overlap))):
        sample_data = data[max(0, j):max(0, j) + fft_bin_size]
        if (len(sample_data) == fft_bin_size):  # if there are enough audio points left to create a full fft bin
            intensities = np.abs(np.fft.fft(sample_data))  # intensities is list of fft results
            box_x = x // box_width
            for y in range(len(intensities) // 2):
                box_y = y // box_height
                # x: corresponding to time
                # y: corresponding to freq
                boxes[(box_x, box_y)].append((intensities[y], x, y))
                if len(boxes[(box_x, box_y)]) > maxes_per_box:
                    boxes[(box_x, box_y)].remove(min(boxes[(box_x, box_y)]))
    #
    for box_x, box_y in list(boxes.keys()):
        for intensity, x, y in boxes[(box_x, box_y)]:
            freqs_dict[y].append(x)

    del boxes
    return freqs_dict


def _find_delay(
    freqs_dict_orig, freqs_dict_sample,
    min_delay=float('nan'),
    max_delay=float('nan')):
    #
    keys = set(freqs_dict_sample.keys()) & set(freqs_dict_orig.keys())
    #
    if not keys:
        raise Exception(
            """I could not find a match. Consider giving a large value to \
"max_misalignment" if the target medias are sure to shoot the same event.""")
    #
    t_diffs = defaultdict(int)
    for key in keys:
        for x_i in freqs_dict_sample[key]:  # determine time offset
            for x_j in freqs_dict_orig[key]:
                delta_t = x_i - x_j
                mincond_ok = math.isnan(min_delay) or delta_t >= min_delay
                maxcond_ok = math.isnan(max_delay) or delta_t <= max_delay
                inc = 1 if mincond_ok and maxcond_ok else 0
                t_diffs[delta_t] += inc

    t_diffs_sorted = sorted(list(t_diffs.items()), key=lambda x: x[1])
    # _logger.debug(t_diffs_sorted)
    time_delay = t_diffs_sorted[-1][0]

    return time_delay


class SyncDetector(object):
    def __init__(self, sample_rate=48000, dont_cache=False):
        self._working_dir = tempfile.mkdtemp()
        self._sample_rate = sample_rate
        self._dont_cache = dont_cache
        self._orig_infos = {}  # per filename

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        retry = 3
        while retry > 0:
            try:
                shutil.rmtree(self._working_dir)
                break
            except:
                import time
                retry -= 1
                time.sleep(1)

    def _extract_audio(self, sample_rate, video_file, duration, afilter):
        """
        Extract audio from video file, save as wav auido file

        INPUT: Video file, and its index of input file list
        OUTPUT: Does not return any values, but saves audio as wav file
        """
        return communicate.media_to_mono_wave(
            video_file, self._working_dir,
            duration=duration,
            sample_rate=sample_rate,
            afilter=afilter)

    def _get_media_info(self, fn):
        if fn not in self._orig_infos:
            self._orig_infos[fn] = communicate.get_media_info(fn)
        return self._orig_infos[fn]

    def _align(self, sample_rate, files, fft_bin_size, overlap, box_height, box_width, samples_per_box,
               max_misalignment, known_delay_map, afilter):
        """
        Find time delays between video files
        """
        def _each(idx):
            maxmisal = 0
            if max_misalignment:
                # max_misalignment only cuts out the media. After cutting out,
                # we need to decide how much to investigate, If there really is
                # a delay close to max_misalignment indefinitely, for true delay
                # detection, it is necessary to cut out and investigate it with
                # a value slightly larger than max_misalignment. This can be
                # thought of as how many loops in _mk_freq_trans_summary should
                # be minimized.
                #(fft_bin_size - overlap) / sample_rate
                maxmisal = max_misalignment
                maxmisal += 512 * ((fft_bin_size - overlap) / sample_rate)
                #_logger.debug(maxmisal)

            exaud_args = dict(
                sample_rate=sample_rate, video_file=files[idx],
                duration=maxmisal,
                afilter=afilter)
            # First, try getting from cache.
            ck = None
            if not self._dont_cache:
                for_cache = dict(exaud_args)
                for_cache.update(dict(
                        fft_bin_size=fft_bin_size,
                        overlap=overlap,
                        box_height=box_height,
                        box_width=box_width,
                        samples_per_box=samples_per_box,
                        atime=os.path.getatime(files[idx])
                        ))
                ck = _cache.make_cache_key(**for_cache)
                cv = _cache.get("_align", ck)
                if cv:
                    return cv[1]
            else:
                _cache.clean("_align")

            # Not found in cache.
            wavfile = self._extract_audio(**exaud_args)
            raw_audio, rate = communicate.read_audio(wavfile)
            ft_dict = _mk_freq_trans_summary(
                raw_audio,
                fft_bin_size, overlap,
                box_height, box_width, samples_per_box)  # bins, overlap, box height, box width
            del raw_audio
            if not self._dont_cache:
                _cache.set("_align", ck, (rate, ft_dict))
            return ft_dict
        #
        samps_per_sec = self._sample_rate / float(fft_bin_size)
        ftds = {i: _each(i) for i in range(len(files))}
        _result1, _result2 = {}, {}
        for kdm_key in known_delay_map.keys():
            kdm = known_delay_map[kdm_key]
            try:
                it = files.index(os.path.abspath(kdm_key))
                ib = files.index(os.path.abspath(kdm["base"]))
            except ValueError:  # simply ignore
                continue
            delay = _find_delay(
                ftds[ib], ftds[it],
                kdm.get("min", float("nan")) * samps_per_sec,
                kdm.get("max", float("nan")) * samps_per_sec)
            _result1[(ib, it)] = -delay / samps_per_sec
        #
        _result2[(0, 0)] = 0.0
        for i in range(len(files) - 1):
            if (0, i + 1) in _result1:
                _result2[(0, i + 1)] = _result1[(0, i + 1)]
            elif (i + 1, 0) in _result1:
                _result2[(0, i + 1)] = -_result1[(i + 1, 0)]
            else:
                delay = _find_delay(ftds[0], ftds[i + 1])
                _result2[(0, i + 1)] = -delay / samps_per_sec
        #        [0, 1], [0, 2], [0, 3]
        # known: [1, 2]
        # _______________^^^^^^[0, 2] must be calculated by [0, 1], and [1, 2]
        # 
        # known: [1, 2], [2, 3]
        # _______________^^^^^^[0, 2] must be calculated by [0, 1], and [1, 2]
        # _______________^^^^^^^^[0, 3] must be calculated by [0, 2], and [2, 3]
        for ib, it in sorted(_result1.keys()):
            for i in range(len(files) - 1):
                if ib > 0 and it == i + 1 and (0, i + 1) not in _result1 and (i + 1, 0) not in _result1:
                    _result2[(0, it)] = _result2[(0, ib)] - _result1[(ib, it)]
                elif it > 0 and ib == i + 1 and (0, i + 1) not in _result1 and (i + 1, 0) not in _result1:
                    _result2[(0, ib)] = _result2[(0, it)] + _result1[(ib, it)]

        # build result
        result = np.array([_result2[k] for k in sorted(_result2.keys())])
        pad_pre = result - result.min()
        trim_pre = -(pad_pre - pad_pre.max())
        #
        return pad_pre, trim_pre

    def get_media_info(self, files):
        """
        Get information about the media (by calling ffprobe).

        Originally the "align" method had been internally acquired to get
        "pad_post" etc. When trying to implement editing processing of a
        real movie, it is very frequent to want to know these information
        (especially duration) in advance. Therefore we decided to release
        this as a method of this class. Since the retrieved result is held
        in the instance variable of class, there is no need to worry about
        performance.
        """
        return [self._get_media_info(fn) for fn in files]

    def align(self, files, fft_bin_size=1024, overlap=0, box_height=512, box_width=43, samples_per_box=7,
              max_misalignment=0, known_delay_map={}, afilter=""):
        """
        Find time delays between video files
        """
        pad_pre, trim_pre = self._align(
            self._sample_rate, files, fft_bin_size, overlap, box_height, box_width, samples_per_box,
            max_misalignment, known_delay_map, afilter)
        #
        infos = self.get_media_info(files)
        orig_dur = np.array([inf["duration"] for inf in infos])
        strms_info = [
            (inf["streams"], inf["streams_summary"]) for inf in infos]
        pad_post = list(
            (pad_pre + orig_dur).max() - (pad_pre + orig_dur))
        trim_post = list(
            (orig_dur - trim_pre) - (orig_dur - trim_pre).min())
        #
        return [{
                "trim": trim_pre[i],
                "pad": pad_pre[i],
                "orig_duration": orig_dur[i],
                "trim_post": trim_post[i],
                "pad_post": pad_post[i],
                "orig_streams": strms_info[i][0],
                "orig_streams_summary": strms_info[i][1],
                }
                for i in range(len(files))]

    @staticmethod
    def summarize_stream_infos(result_from_align):
        """
        This is a service function that calculates several summaries on
        information about streams of all medias returned by
        SyncDetector#align.

        Even if "align" has only detectable delay information, you are
        often in trouble. This is because editing for lineup of targeted
        plural media involves unification of sampling rates (etc) in many
        cases.

        Therefore, this function calculates the maximum sampling rate etc.
        through all files, and returns it in a dictionary format.
        """
        result = dict(
            max_width=0,
            max_height=0,
            max_sample_rate=0,
            max_fps=0.0,
            has_video = [],
            has_audio = [])
        for ares in result_from_align:
            summary = ares["orig_streams_summary"]  # per single media

            result["max_width"] = max(
                result["max_width"], summary["max_resol_width"])
            result["max_height"] = max(
                result["max_height"], summary["max_resol_height"])
            result["max_sample_rate"] = max(
                result["max_sample_rate"], summary["max_sample_rate"])
            result["max_fps"] = max(
                result["max_fps"], summary["max_fps"])

            result["has_video"].append(
                summary["num_video_streams"] > 0)
            result["has_audio"].append(
                summary["num_audio_streams"] > 0)
        return result


def _bailout(parser):
    parser.print_help()
    sys.exit(1)


def main(args=sys.argv):
    import argparse

    parser = argparse.ArgumentParser(description="""\
This program reports the offset difference for audio and video files,
containing audio recordings from the same event. It relies on ffmpeg being
installed and the python libraries scipy and numpy.""")
    parser.add_argument(
        '--max_misalignment',
        type=str, default="1800",
        help="""When handling media files with long playback time,
it may take a huge amount of time and huge memory.
In such a case, by changing this value to a small value,
it is possible to indicate the scanning range of the media file to the program.
(default: %(default)s)""")
    parser.add_argument(
        '--known_delay_map',
        type=str,
        default="{}",
        help='''\
Delay detection by feature comparison of frequency intensity may be wrong.
Since it is an approach that takes only one maximum value of the delay 
which can best explain the difference in the intensity distribution, if 
it happens to have a range where characteristics are similar, it adopts it 
by mistake. "known_delay_map" is a mechanism for forcing this detection
error manually. For example, if the detection process returns 3 seconds
despite knowing that the delay is greater than at least 20 minutes,
you can complain with using "known_delay_map" like "It's over 20 minutes!".
Please pass it in JSON format, like 
'{"foo.mp4": {"min": 120, "max": 140, "base": "bar.mp4"}}'
Specify the adjustment as to which media is adjusted to "base", the minimum and 
maximum delay as "min", "max". The values of "min", "max"
are the number of seconds.''')
    parser.add_argument(
        '--sample_rate',
        type=int,
        default=48000,
        help='''In this program, delay is examined by unifying all the sample rates \
of media files into the same one. If this value is the value itself of the media file \
itself, the result will be more precise. However, this wastes a lot of memory, so you \
can reduce memory consumption by downsampling (instead losing accuracy a bit). \
The default value uses quite a lot of memory, but if it changes to a value of, for example, \
44100, 22050, etc., although a large error of about several tens of milliseconds \
increases, the processing time is greatly shortened. (default: %(default)d)''')
    parser.add_argument(
        '--dont_cache',
        action="store_true",
        help='''Normally, this script stores the result in cache ("%s"). \
If you hate this behaviour, specify this option.''' % (
            _cache.cache_root_dir))
    parser.add_argument(
        '--json',
        action="store_true",
        help='To report in json format.',)
    parser.add_argument(
        'file_names',
        nargs="+",
        help='Media files including audio streams. \
It is possible to pass any media that ffmpeg can handle.',)
    args = parser.parse_args(args[1:])
    known_delay_map = json.loads(args.known_delay_map)

    logging.basicConfig(
        level=logging.DEBUG,
        stream=sys.stderr,
        format="%(created)f|%(levelname)5s:%(module)s#%(funcName)s:%(message)s")

    file_specs = check_and_decode_filenames(
        args.file_names, min_num_files=2)
    if not file_specs:
        _bailout(parser)

    with SyncDetector(
        sample_rate=args.sample_rate,
        dont_cache=args.dont_cache) as det:
        result = det.align(
            file_specs,
            max_misalignment=communicate.parse_time(args.max_misalignment),
            known_delay_map=known_delay_map)
    if args.json:
        print(json.dumps(
                {'edit_list': list(zip(file_specs, result))}, indent=4, sort_keys=True))
    else:
        report = []
        for i, path in enumerate(file_specs):
            if not (result[i]["trim"] > 0):
                continue
            report.append(
                """Result: The beginning of '%s' needs to be trimmed off %.4f seconds \
(or to be added %.4f seconds padding) for all files to be in sync""" % (
                    path, result[i]["trim"], result[i]["pad"]))
        if report:
            print("\n".join(report))
        else:
            print("files are in sync already")


if __name__ == "__main__":
    main()
