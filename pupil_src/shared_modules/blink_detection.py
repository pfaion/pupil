'''
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2018 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
'''

from plugin import Analysis_Plugin_Base
from pyglui import ui, cygl
from collections import deque
import numpy as np
import OpenGL.GL as gl

from pyglui.cygl.utils import *
import gl_utils

import logging
logger = logging.getLogger(__name__)


class Blink_Detection(Analysis_Plugin_Base):
    """
    This plugin implements a blink detection algorithm, based on sudden drops in the
    pupil detection confidence.
    """
    order = .8
    icon_chr = chr(0xe81a)
    icon_font = 'pupil_icons'

    def __init__(self, g_pool, history_length=0.2, onset_confidence_threshold=0.5, offset_confidence_threshold=0.5, visualize=True):
        super().__init__(g_pool)
        self.visualize = visualize
        self.history_length = history_length  # unit: seconds
        self.onset_confidence_threshold = onset_confidence_threshold
        self.offset_confidence_threshold = offset_confidence_threshold

        self.history = deque()
        self.menu = None
        self._recent_blink = None

    def init_ui(self):
        self.add_menu()
        self.menu.label = 'Blink Detector'
        self.menu.append(ui.Info_Text('This plugin detects blink on- and offsets based on confidence drops.'))
        self.menu.append(ui.Switch('visualize', self, label='Visualize'))
        self.menu.append(ui.Slider('history_length', self,
                                   label='Filter length [seconds]',
                                   min=0.1, max=.5, step=.05))
        self.menu.append(ui.Slider('onset_confidence_threshold', self,
                                   label='Onset confidence threshold',
                                   min=0., max=1., step=.05))
        self.menu.append(ui.Slider('offset_confidence_threshold', self,
                                   label='Offset confidence threshold',
                                   min=0., max=1., step=.05))

    def deinit_ui(self):
        self.remove_menu()

    def recent_events(self, events={}):
        events['blinks'] = []
        self._recent_blink = None
        self.history.extend(events.get('pupil_positions', []))

        try:  # use newest gaze point to determine age threshold
            age_threshold = self.history[-1]['timestamp'] - self.history_length
            while self.history[1]['timestamp'] < age_threshold:
                self.history.popleft()  # remove outdated gaze points
        except IndexError:
            pass

        filter_size = len(self.history)
        if filter_size < 2 or self.history[-1]['timestamp'] - self.history[0]['timestamp'] < self.history_length:
            return

        activity = np.fromiter((pp['confidence'] for pp in self.history), dtype=float)
        blink_filter = np.ones(filter_size) / filter_size
        blink_filter[filter_size // 2:] *= -1

        # The theoretical response maximum is +-0.5
        # Response of +-0.45 seems sufficient for a confidence of 1.
        filter_response = activity @ blink_filter / 0.45

        if -self.offset_confidence_threshold <= filter_response <= self.onset_confidence_threshold:
            return  # response cannot be classified as blink onset or offset
        elif filter_response > self.onset_confidence_threshold:
            blink_type = 'onset'
        else:
            blink_type = 'offset'

        confidence = min(abs(filter_response), 1.)  # clamp conf. value at 1.
        logger.debug('Blink {} detected with confidence {:0.3f}'.format(blink_type, confidence))
        # Add info to events
        blink_entry = {
            'topic': 'blink',
            'type': blink_type,
            'confidence': confidence,
            'base_data': list(self.history),
            'timestamp': self.history[len(self.history)//2]['timestamp'],
            'record': True
        }
        events['blinks'].append(blink_entry)
        self._recent_blink = blink_entry

    def gl_display(self):
        if self._recent_blink and self.visualize:
            if self._recent_blink['type'] == 'onset':
                cygl.utils.push_ortho(1, 1)
                cygl.utils.draw_gl_texture(np.zeros((1, 1, 3), dtype=np.uint8),
                                           alpha=self._recent_blink['confidence'] * 0.5)
                cygl.utils.pop_ortho()

    def get_init_dict(self):
        return {'history_length': self.history_length, 'visualize': self.visualize,
                'onset_confidence_threshold': self.onset_confidence_threshold,
                'offset_confidence_threshold': self.offset_confidence_threshold}


class Offline_Blink_Detection(Blink_Detection):
    def __init__(self, g_pool, history_length=0.2, onset_confidence_threshold=0.5,
                 offset_confidence_threshold=0.5, visualize=True):
        self._history_length = None
        self._onset_confidence_threshold = None
        self._offset_confidence_threshold = None

        super().__init__(g_pool, history_length, onset_confidence_threshold,
                         offset_confidence_threshold, visualize)
        self.filter_response = []
        self.response_classification = []
        self.timestamps = []

    def init_ui(self):
        super().init_ui()
        self.timeline = ui.Timeline('Blink Detection', self.draw_activation)
        self.timeline.height *= 1.5
        self.g_pool.user_timelines.append(self.timeline)

    def deinit_ui(self):
        super().deinit_ui()
        self.g_pool.user_timelines.remove(self.timeline)
        self.timeline = None

    def recent_events(self, events):
        pass

    def gl_display(self):
        pass

    def on_notify(self, notification):
        if notification['subject'] == 'blink_detection.should_recalculate':
            self.recalculate()
        elif notification['subject'] == 'pupil_positions_changed':
            logger.info('Pupil postions changed. Recalculating.')
            self.recalculate()
        elif notification['subject'] == "should_export":
            self.export(notification['range'], notification['export_dir'])

    def export(self, export_range, export_dir):
        pass

    def recalculate(self):
        import time
        t0 = time.time()
        all_pp = self.g_pool.pupil_positions
        conf_iter = (pp['confidence'] for pp in all_pp)
        activity = np.fromiter(conf_iter, dtype=float, count=len(all_pp))
        total_time = all_pp[-1]['timestamp'] - all_pp[0]['timestamp']
        filter_size = round(len(all_pp) * self.history_length / total_time)
        blink_filter = np.ones(filter_size) / filter_size
        blink_filter[filter_size // 2:] *= -1
        self.timestamps = [pp['timestamp'] for pp in all_pp]

        # The theoretical response maximum is +-0.5
        # Response of +-0.45 seems sufficient for a confidence of 1.
        self.filter_response = np.convolve(activity, blink_filter, 'same') / 0.45

        onsets = self.filter_response > self.onset_confidence_threshold
        offsets = self.filter_response < -self.onset_confidence_threshold

        self.response_classification = np.zeros(self.filter_response.shape)
        self.response_classification[onsets] = 1.
        self.response_classification[offsets] = -1.
        self.timeline.refresh()

        tm1 = time.time()
        logger.debug('Recalculating took\n\t{:.4f}sec for {} pp\n\t{} pp/sec\n\tsize: {}'.format(tm1 - t0, len(all_pp), len(all_pp) / (tm1 - t0), filter_size))

    def draw_activation(self, width, height, scale):
        response_points = [(t, r) for t, r in zip(self.timestamps, self.filter_response)]
        class_points = [(t, r) for t, r in zip(self.timestamps, self.response_classification)]
        if len(response_points) == 0:
            return

        t0, t1 = self.g_pool.timestamps[0], self.g_pool.timestamps[-1]
        thresholds = [(t0, self.onset_confidence_threshold),
                      (t1, self.onset_confidence_threshold),
                      (t0, -self.offset_confidence_threshold),
                      (t1, -self.offset_confidence_threshold)]

        with gl_utils.Coord_System(t0, t1, 1, -1):
            draw_polyline(response_points, color=RGBA(0.6602, 0.8594, 0.4609, 0.8),
                          line_type=gl.GL_LINE_STRIP, thickness=1*scale)
            draw_polyline(class_points, color=RGBA(0.9961, 0.3789, 0.5313, 0.8),
                          line_type=gl.GL_LINE_STRIP, thickness=1*scale)
            draw_polyline(thresholds, color=RGBA(0.9961, 0.8438, 0.3984, 0.8),
                          line_type=gl.GL_LINES, thickness=1*scale)

    @property
    def history_length(self):
        return self._history_length

    @history_length.setter
    def history_length(self, val):
        if self._history_length != val:
            self.notify_all({'subject': 'blink_detection.should_recalculate', 'delay': .2})
        self._history_length = val

    @property
    def onset_confidence_threshold(self):
        return self._onset_confidence_threshold

    @onset_confidence_threshold.setter
    def onset_confidence_threshold(self, val):
        if self._onset_confidence_threshold != val:
            self.notify_all({'subject': 'blink_detection.should_recalculate', 'delay': .2})
        self._onset_confidence_threshold = val

    @property
    def offset_confidence_threshold(self):
        return self._offset_confidence_threshold

    @offset_confidence_threshold.setter
    def offset_confidence_threshold(self, val):
        if self._offset_confidence_threshold != val:
            self.notify_all({'subject': 'blink_detection.should_recalculate', 'delay': .2})
        self._offset_confidence_threshold = val
