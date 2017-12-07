"""
Module to handle tracking of objects in thermal video.
"""

# we need to use a non GUI backend.  AGG works but is quite slow so I used SVG instead.
import matplotlib
matplotlib.use("SVG")

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import matplotlib.animation as manimation

import numpy as np
import cv2

from cptv import CPTVReader

import pytz
import datetime
import dateutil

from Classifier import Classifier, Segment

import os
import json
import pickle
import gzip

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
            # Let the base class default method raise the TypeError
            return json.JSONEncoder.default(self, obj)
        if isinstance(obj, TrackingFrame):
            return (int(obj.left), int(obj.top), int(obj.right), int(obj.bottom))
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)


def load_tracker_stats(filename):
    """
    Loads a stats file for a processed clip.
    :param filename: full path and filename to stats file
    :return: returns the stats file
    """

    with open(filename, 'r') as t:
        # add in some metadata stats
        stats = json.load(t)

    stats['date_time'] = dateutil.parser.parse(stats['date_time'])
    return stats


def load_track_stats(filename):
    """
    Loads a stats file for a processed track.
    :param filename: full path and filename to stats file
    :return: returns the stats file
    """

    with open(filename, 'r') as t:
        # add in some metadata stats
        stats = json.load(t)

    stats['timestamp'] = dateutil.parser.parse(stats['timestamp'])
    return stats


def apply_threshold(frame, threshold = 50.0):
    """ Creates a binary mask out of an image by applying a threshold.
        Any pixels more than the threshold are set 1, all others are set to 0.
        A blur is also applied as a filtering step
    """

    thresh = cv2.GaussianBlur(frame, (5,5), 0) - threshold
    thresh[thresh < 0] = 0
    thresh[thresh > 0] = 1
    return thresh


def get_image_subsection(image, bounds, window_size):
    """
    Returns a subsection of the original image bounded by bounds.
    Area outside of frame will be filled by repeating edge pixels
    """

    # cropping method.  just center on the bounds center and take a section there.

    if len(image.shape) == 2:
        image = image[:,:,np.newaxis]

    padding = 40

    midx = int(bounds.mid_x + padding)
    midy = int(bounds.mid_y + padding)

    window_half_width, window_half_height = window_size[0] // 2, window_size[1] // 2

    image_height, image_width, channels = image.shape

    enlarged_frame = np.pad(image, [(padding, padding), (padding, padding), (0,0)], mode='edge')

    sub_section = enlarged_frame[midy-window_half_width:midy+window_half_width, midx-window_half_width:midx+window_half_width]

    if channels == 1:
        sub_section = sub_section[:,:,0]

    return sub_section


def normalise(x):
    x = x.astype(np.float32)
    return (x - np.mean(x)) / max(0.000001, float(np.std(x)))

class TrackingFrame:
    """ Defines a rectangle by the topleft point and width / height. """
    def __init__(self, topleft_x, topleft_y, width, height, mass = 0):
        """ Defines new rectangle. """
        self.x = topleft_x
        self.y = topleft_y
        self.width = width
        self.height = height
        self.mass = mass

    def copy(self):
        return TrackingFrame(self.x, self.y, self.width, self.height, self.mass)

    @property
    def mid_x(self):
        return self.x + self.width / 2

    @property
    def mid_y(self):
        return self.y + self.height / 2

    @property
    def left(self):
        return self.x

    @property
    def top(self):
        return self.y

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def overlap_area(self, other):
        """ Compute the area overlap between this rectangle and another. """
        x_overlap = max(0, min(self.right, other.right) - max(self.left, other.left))
        y_overlap = max(0, min(self.bottom, other.bottom) - max(self.top, other.top))
        return x_overlap * y_overlap

    @property
    def area(self):
        return self.width * self.height

    def __repr__(self):
        return "({0},{1},{2},{3})".format(self.left, self.top, self.right, self.bottom)

    def __str__(self):
        return "<({0},{1})-{2}x{3}>".format(self.x, self.y, self.width, self.height)


class Track:
    """ Defines an object tracked through the video frames."""

    """ keeps track of which id number we are up to."""
    _track_id = 1

    """ There is no target. """
    TARGET_NONE = 'none'

    """ Target has been acquired."""
    TARGET_ACQUIRED = 'acquired'

    """ Target has been lost."""
    TARGET_LOST = 'lost'

    """ Target collided with another target, or split."""
    TARGET_SPLIT = 'split'

    def __init__(self, x, y, width, height, mass = 0):

        self.bounds = TrackingFrame(x, y, width, height)
        self.status = Track.TARGET_NONE
        self.origin = (self.bounds.mid_x, self.bounds.mid_y)
        self.first_frame = 0

        self.id = Track._track_id
        Track._track_id += 1

        self.vx = 0.0
        self.vy = 0.0

        self.mass = mass

        # history for each frame in track
        self.mass_history = []

        self.bounds_history = []

        # used to record prediction of what kind of animal we are tracking
        self.prediction_history = []

        # average mass
        self.average_mass = 0.0
        # how much this track has moved
        self.movement = 0.0
        # how many pixels we have moved from origin
        self.max_offset = 0.0
        # how likely this is to be a valid track
        self.score = 0.0

    def __repr__(self):
        return "({0},{1})".format(self.bounds.x, self.bounds.y)

    @property
    def offsetx(self):
        """ Offset from where object was originally detected. """
        return self.bounds.mid_x - self.origin[0]

    @property
    def offsety(self):
        """ Offset from where object was originally detected. """
        return self.bounds.mid_x - self.origin[0]

    def sync_new_location(self, regions_of_interest):
        """ Work out out estimated new location for the frame using last position
            and movement vectors as an initial guess. """
        gx = int(self.bounds.x + self.vx)
        gy = int(self.bounds.y + self.vy)

        new_bounds = TrackingFrame(gx, gy, self.bounds.width, self.bounds.height)

        # look for regions and calculate their overlap
        similar_regions = []
        overlapping_regions = []
        for region in regions_of_interest:
            overlap_fraction = (new_bounds.overlap_area(region) * 2) / (new_bounds.area + region.area)
            relative_area_difference = abs(region.area - new_bounds.area) / new_bounds.area
            if overlap_fraction > 0.10 and relative_area_difference < 0.5:
                similar_regions.append(region)
            if overlap_fraction > 0.10:
                overlapping_regions.append(region)

        if len(similar_regions) == 0:
            # lost target!
            self.status = Track.TARGET_LOST
        elif len(similar_regions) >= 2:
            # target split
            self.status = Track.TARGET_SPLIT
        else:
            # just follow target.
            old_x, old_y = self.bounds.mid_x, self.bounds.mid_y
            self.status = Track.TARGET_ACQUIRED
            self.bounds.x = similar_regions[0].x
            self.bounds.y = similar_regions[0].y
            self.bounds.width = similar_regions[0].width
            self.bounds.height = similar_regions[0].height
            self.mass = similar_regions[0].mass
            # print("move to ",self.bounds.x, self.bounds.y)

            # work out out new velocity
            new_vx = self.bounds.mid_x - old_x
            new_vy = self.bounds.mid_y - old_y
            # smooth out the velocity changes a little bit.
            smooth = 0.9  # ema smooth
            self.vx = smooth * self.vx + (1 - smooth) * new_vx
            self.vy = smooth * self.vy + (1 - smooth) * new_vy

        return overlapping_regions




class Tracker:
    """ Tracks objects within a CPTV thermal video file. """

    # these should really be in some kind of config file...

    # size of tracking window output in pixels.
    WINDOW_SIZE = 64

    # dpi to use for video, 100 is default, 50 is faster but hard to see tracking windows.
    VIDEO_DPI = 100

    # If enabled removes background by subtracting out the average pixels values before filtering.
    # Set to True to enable, False to disable, and 'auto' to enable only on stationary clips.

    USE_BACKGROUND_SUBTRACTION = 'auto'

    # auto threshold needs to find a near maximum value to calculate the threshold level
    # a better solution might be the mean of the max of each frame?
    THRESHOLD_PERCENTILE = 99.9

    # the coldest value to display when rendering previews
    TEMPERATURE_MIN = 2800
    TEMPERATURE_MAX = 4200

    # any clips with a mean temperature hotter than this will be excluded
    MAX_MEAN_TEMPERATURE_THRESHOLD = 3800

    # any clips with a temperature dynamic range greater than this will be excluded
    MAX_TEMPERATURE_RANGE_THRESHOLD = 2000

    # if the mean pixel change is below this threshold then classify the video as having a static background
    STATIC_BACKGROUND_THRESHOLD = 5.0

    def __init__(self, full_path):
        """
        Create a Tracker object
        :param full_path: path and filename of CPTV file to process
        """

        self.frames = []
        self.track_history = {}
        self.load(open(full_path, 'rb'))
        self.tag = "UNKNOWN"
        self.source = os.path.split(full_path)[1]
        self.tracks = []

        # class used to write MPEG videos, must be set to enable MPEG video output
        self.MPEGWriter = None

        # if enabled tracker will try and predict what animals are in each track
        self.include_prediction = False

        # the classifer to use to classify tracks
        self.classifier = None

        # find background
        self.background, self.auto_threshold = self.get_background()
        self.average_background_delta = self.get_background_average_change()
        self.is_static_background = self.average_background_delta < Tracker.STATIC_BACKGROUND_THRESHOLD

        # If set to a number only this many frames will be used.
        self.max_tracks = None
        self.stats = self._get_clip_stats()


    def _get_clip_stats(self):
        """
        Computes statitics for currently loaded clip and returns a dictionary containing the stats.
        :return: A dictionary containing stats from video clip.
        """
        result = {}
        local_tz = pytz.timezone('Pacific/Auckland')
        result['mean_temp'] = int(np.asarray(self.frames).mean())
        result['max_temp'] = int(np.asarray(self.frames).max())
        result['min_temp'] = int(np.asarray(self.frames).min())
        result['date_time'] = self.video_start_time.astimezone(local_tz)
        result['source'] = self.source
        result['is_static_background'] = self.is_static_background
        result['auto_threshold'] = self.auto_threshold
        result['is_night'] = self.video_start_time.astimezone(local_tz).time().hour >= 21 or self.video_start_time.astimezone(local_tz).time().hour <= 4
        result['average_background_delta'] = self.average_background_delta

        return result

    def print_stats(self):
        self.log_message(" - Temperature:{0} ({1}-{2}), Time of day: {3},Threshold: {4:.1f}".format(
            self.stats['mean_temp'], self.stats['min_temp'], self.stats['max_temp'],
            self.stats['time_of_day'].strftime("%H%M"), self.stats['auto_threshold']))

    def save_stats(self, filename):
        """ Writes stats to file. """

        # we need to convert datetime to a string so it will serialise through json
        with open(filename, 'w') as stats_file:
            json.dump(self.stats, stats_file, indent=4,  cls=CustomJSONEncoder)


    def load(self, source):
        """ Load frames from a CPTV file. """
        reader = CPTVReader(source)
        self.frames = [frame.copy() for (frame, offset) in reader]
        self.video_start_time = reader.timestamp


    def _get_regions_of_interest(self, frame, threshold, erosion=1, include_markers=False):
        """ Returns a list of bounded boxes for all regions of interest in the frame.
            Regions of interest are hotspots that stand out against the background.
        """

        thresh = np.asarray(apply_threshold(frame, threshold=(np.median(frame) + threshold)), dtype=np.uint8)

        # perform erosion
        kernel = np.ones((3, 3), np.uint8)
        eroded = cv2.erode(thresh, kernel, iterations=erosion)
        labels, markers, stats, centroids = cv2.connectedComponentsWithStats(eroded)

        # we enlarge the rects a bit, partly because we eroded them previously, and partly because we want some context.
        padding = 12

        # find regions
        rects = []
        for i in range(1, labels):
            rect = TrackingFrame(stats[i, 0] - padding, stats[i, 1] - padding, stats[i, 2] + padding * 2,
                                 stats[i, 3] + padding * 2, stats[i,4])
            rects.append(rect)

        return (rects, markers) if include_markers else rects

    def _init_classifier(self):
        self.classifier = Classifier('./models/model4b')

    def _init_video(self, title, size):
        """
        Initialise an MPEG video with given title and size

        :param title: Title for the MPEG file
        :param size: tuple containing dims (width, height)
        :param colormap: colormap to use when outputting video

        :return: returns a tuple containing (figure, axis, image, and writer)
        """

        if self.MPEGWriter is None:
            raise Exception("MPEGWriter not assigned, can not initialise video export.")

        metadata = dict(title=title, artist='Cacophony Project')
        writer = self.MPEGWriter(fps=9, metadata=metadata)

        # we create a figure of the appropriate dims.  Assuming 100 dpi
        figure_size = (size[0]/25, size[1]/25)

        fig, ax = plt.subplots(1, figsize = figure_size)
        data = np.zeros((size[1], size[0]),dtype=np.float32)
        ax.axis('off')

        im = plt.imshow(data, vmin=Tracker.TEMPERATURE_MIN , vmax=Tracker.TEMPERATURE_MAX)
        return (fig, ax, im, writer)


    def get_background_average_change(self):
        """
        Returns how much each pixel changes on average over the video.  Used to detect static backgrounds.
        :return: How much each pixel changes in value every frame.
        """
        delta = np.asarray(self.frames[1:],dtype=np.float32) - np.asarray(self.frames[:-1],dtype=np.float32)
        return float(np.mean(np.abs(delta)))


    def get_background(self):
        """
        Returns estimated background for video and threshold used.
        """

        background = np.percentile(np.asarray(self.frames), q=10.0, axis=0)

        deltas = np.reshape(self.frames - background, [-1])
        threshold = np.percentile(deltas, q=Tracker.THRESHOLD_PERCENTILE) / 2

        # cap the threshold to something reasonable
        if threshold < 10.0:
            threshold = 10.0
        if threshold > 50.0:
            threshold = 50.0

        return (background, float(threshold))

    def display(self, filename, colormap = None):
        """
        Exports tracking information to a video file for debugging.
        """

        # Display requires the MPEGWriting to be set.
        if self.MPEGWriter is None:
            raise Exception("Can not generate clip preview as MPEGWriter is not initialized.  Try installing FFMPEG.")

        if colormap is None: colormap = plt.cm.jet

        # setup the writer
        (fig, ax, im, writer) = self._init_video(filename, (160*2, 120*2))
        im.colormap = colormap

        # write video
        frame_number = 0
        with writer.saving(fig, filename, dpi=Tracker.VIDEO_DPI):
            for frame, marked, rects, flow, filtered in zip(self.frames, self.marked_frames, self.regions, self.flow_frames, self.filtered_frames):

                # marked is an image with each pixel's value being the label, 0...n for n objects
                # I multiply it here, but really I should use a seperate color map for this.
                # maybe I could multiply it modulo, and offset by some amount?

                # really should be using a pallete here, I multiply by 10000 to make sure the binary mask '1' values get set to the brightest color (which is about 4000)
                # here I map the flow magnitude [ranges in the single didgits) to a temperature in the display range.
                flow_magnitude = (flow[:,:,0]**2 + flow[:,:,1]**2) ** 0.5
                stacked = np.hstack((np.vstack((frame, marked*10000)),np.vstack((filtered + Tracker.TEMPERATURE_MIN, 200 * flow_magnitude + Tracker.TEMPERATURE_MIN))))
                im.set_data(stacked)

                # items to be removed from image after we draw it (otherwise they turn up there next frame)
                remove_list = []

                # look for any tracks that occur on this frame
                for track in self.tracks:
                    frame_offset = frame_number - track.first_frame
                    if frame_offset > 0 and frame_offset < len(track.bounds_history)-1:

                        # display the track
                        rect = track.bounds_history[frame_offset]
                        patch = patches.Rectangle((rect.x, rect.y), rect.width, rect.height, linewidth=1, edgecolor='r',
                                                  facecolor='none')
                        ax.add_patch(patch)

                        if self.include_prediction:
                            predicted_class = self.classifier.classes[np.argmax(track.prediction_history[frame_offset])]
                            predicted_prob = float(max(track.prediction_history[frame_offset]))
                            if predicted_prob < 0.5:
                                prediction_text = "Unknown [{0:.1f}%]".format(predicted_prob*100)
                            else:
                                prediction_text = "{0} [{1:.1f}%]".format(predicted_class, predicted_prob * 100)
                            text = ax.text(rect.left, rect.bottom + 5, prediction_text, color='white')
                            remove_list.append(text)

                        remove_list.append(patch)

                fig.canvas.draw()
                writer.grab_frame()

                for item in remove_list:
                    item.remove()

                frame_number += 1

        plt.close(fig)


    def extract(self):
        """
        Extract regions of interest from frames, and create some initial tracks.
        """

        if Tracker.USE_BACKGROUND_SUBTRACTION.lower() == 'auto':
            use_background_subtraction = self.is_static_background
        else:
            use_background_subtraction = Tracker.USE_BACKGROUND_SUBTRACTION

        if use_background_subtraction:
            mask, threshold = self.background, self.auto_threshold
        else:
            # just use a blank mask
            mask = np.zeros_like(self.frames[0])
            threshold = 75.0

        active_tracks = []

        # todo: these are needed for the display routine, should really declare them in init or something.
        self.regions = []
        self.marked_frames = []
        self.filtered_frames = []
        self.flow_frames = []

        # don't process clips that are too hot.
        if self.stats['mean_temp'] > Tracker.MAX_MEAN_TEMPERATURE_THRESHOLD:
            return

        # don't process clips with too hot a temperature difference
        if self.stats['max_temp'] - self.stats['min_temp'] > Tracker.MAX_TEMPERATURE_RANGE_THRESHOLD :
            return

        Track._track_id = 1

        tvl1 = cv2.createOptFlow_DualTVL1()

        for frame_number, frame in enumerate(self.frames):

            # find regions of interest in this frame
            new_regions, markers = self._get_regions_of_interest(frame - mask, threshold, include_markers=True)

            self.marked_frames.append(markers)

            # create a filtered frame
            filtered = frame - mask
            filtered = filtered - np.median(filtered)
            filtered[filtered < 0] = 0
            self.filtered_frames.append(filtered)

            # calculate optical flow (might be better to use DualTVL1 algorithm
            flow = np.zeros([frame.shape[0], frame.shape[1], 2], dtype=np.uint8)
            if len(self.filtered_frames) >= 2:
                prev_gray_frame = self.filtered_frames[-2].astype(np.uint8)
                current_gray_frame = self.filtered_frames[-1].astype(np.uint8)
                flow = tvl1.calc(prev_gray_frame, current_gray_frame, flow)

            flow = flow.astype(np.float16)
            self.flow_frames.append(flow)

            used_regions = []

            # step 2. match these with tracked objects
            for track in active_tracks:
                # update each track.
                used_regions = used_regions + track.sync_new_location(new_regions)

            # step 3. create new tracks for any unmatched regions
            for region in new_regions:
                if region in used_regions:
                    continue
                track = Track(region.x, region.y, region.width, region.height, region.mass)
                track.first_frame = frame_number
                active_tracks.append(track)
                self.track_history[track] = []

            active_tracks = [track for track in active_tracks if track.status != Track.TARGET_LOST]

            self.regions.append([track.bounds.copy() for track in active_tracks])

            # step 5. record history.
            for track in active_tracks:
                self.track_history[track].append(
                    (frame_number, track.bounds.copy(), (track.vx, track.vy), (track.offsetx, track.offsety), track.mass))

        self.tracks = self.track_history.keys()
        self.get_tracks_statistics()

    def filter_tracks(self):
        """ Removes tracks with too poor a score to be used. """
        if self.max_tracks is not None:
            print(" -using only {0} tracks out of {1}".format(self.max_tracks, len(self.tracks)))
            self.tracks = self.tracks[:self.max_tracks]

    def get_tracks_statistics(self):
        """ Record stats on each track, including assigning it a score.  Also sorts tracks by score and filters out
            poor tracks. """

        track_scores = []

        counter = 1
        for track in self.tracks:

            history = self.track_history[track]

            track_length = len(history)

            # calculate movement statistics
            track.movement = sum(
                (vx ** 2 + vy ** 2) ** 0.5 for (frame_number, bounds, (vx, vy), (dx, dy), mass) in history)
            track.max_offset = max(
                (dx ** 2 + dy ** 2) ** 0.5 for (frame_number, bounds, (vx, vy), (dx, dy), mass) in history)

            track.score = track.movement + track.max_offset

            track.mass_history = list([int(mass) for (frame_number, bounds, (vx, vy), (dx, dy), mass) in history])
            track.average_mass = np.mean(track.mass_history)

            track.duration = track_length / 9.0

            # discard any tracks that are less than 3 seconds long (27 frames)
            # these are probably glitches anyway, or don't contain enough information.
            if track_length < 9 * 3:
                continue

            # discard tracks that do not move enough
            if track.max_offset < 4.0:
                continue

            track_scores.append((track.score, track))

            counter += 1

        track_scores.sort(reverse=True)
        self.tracks = [track for (score, track) in track_scores]

    def export(self, filename, use_compression=False, include_track_previews=False):
        """
        Export tracks to given filename base.  An MPEG and TRK file will be exported.
        :param filename: full path and filename to export track to
        :param use_compression: if enabled will gzip track
        """

        # todo: would be great to just have a proper segment class that handles most of the code in this function...

        if include_track_previews and self.MPEGWriter is None:
            raise Exception("Track previews require MPEGWriter to be initialized.")

        if self.include_prediction and self.classifier is None:
            print("Loading classfication model.")
            self._init_classifier()

        base_filename = os.path.splitext(filename)[0]

        self.filter_tracks()

        # create segments
        segment = Segment()

        for counter, track in enumerate(self.tracks):

            history = self.track_history[track]

            MPEG_filename = base_filename + "-" + str(counter+1 ) + ".mp4"
            TRK_filename = base_filename + "-" + str(counter+1) + ".trk"
            Stats_filename = base_filename + "-" + str(counter+1) + ".txt"

            # export frames
            window_frames = []
            filtered_frames = []
            flow_frames = []
            motion_vectors = []

            # export a MPEG preview of the track
            if include_track_previews:
                (fig, ax, im, writer) = self._init_video(MPEG_filename, (Tracker.WINDOW_SIZE, Tracker.WINDOW_SIZE))
                with writer.saving(fig, MPEG_filename, dpi=Tracker.VIDEO_DPI):
                    for frame_number, bounds, (vx, vy), (dx, dy), mass in history:
                        # get a frame to be used for the preview
                        draw_frame = get_image_subsection(self.filtered_frames[frame_number], bounds,
                                                          (Tracker.WINDOW_SIZE, Tracker.WINDOW_SIZE))
                        draw_frame = 5 * draw_frame + Tracker.TEMPERATURE_MIN

                        im.set_data(draw_frame)
                        fig.canvas.draw()
                        writer.grab_frame()

                plt.close(fig)

            # export the track file
            for frame_number, bounds, (vx, vy), (dx, dy), mass in history:

                window_frames.append(get_image_subsection(self.frames[frame_number], bounds, (Tracker.WINDOW_SIZE, Tracker.WINDOW_SIZE)))
                filtered_frames.append(get_image_subsection(self.filtered_frames[frame_number], bounds, (Tracker.WINDOW_SIZE, Tracker.WINDOW_SIZE)))
                flow_frames.append(get_image_subsection(self.flow_frames[frame_number], bounds, (Tracker.WINDOW_SIZE, Tracker.WINDOW_SIZE)))

                motion_vectors.append((vx, vy))

                track.bounds_history.append(bounds.copy())

                if self.include_prediction:
                    data = np.zeros([64, 64, 4], dtype=np.float32)
                    data[:, :, 0] = normalise(window_frames[-1])
                    data[:, :, 1] = normalise(filtered_frames[-1])
                    data[:, :, 2:3+1] = flow_frames[-1]
                    segment.append_frame(data)
                    track.prediction_history.append(self.classifier.predict(segment))


            # export track stats.
            save_file = {}
            save_file['track_id'] = track.id
            save_file['frames'] = window_frames
            save_file['filtered_frames'] = filtered_frames
            save_file['flow_frames'] = flow_frames
            save_file['motion_vectors'] = motion_vectors

            stats = {}

            stats['id'] = track.id
            stats['score'] = track.score
            stats['movement'] = track.movement
            stats['average_mass'] = track.average_mass
            stats['max_offset'] = track.max_offset
            stats['timestamp'] = self.video_start_time
            stats['duration'] = track.duration
            stats['tag'] = self.tag
            stats['origin'] = track.origin
            stats['filename'] = self.source
            stats['threshold'] = self.auto_threshold
            stats['confidence'] = self.stats['confidence']
            stats['is_static_background'] = self.is_static_background
            stats['mass_history'] = track.mass_history
            stats['bounds_history'] = track.bounds_history

            if len(track.mass_history) != len(window_frames):
                print("mass history mismatch", len(track.mass_history), len(window_frames))

            # save out track data
            if use_compression:
                pickle.dump(save_file, gzip.open(TRK_filename, 'wb'))
            else:
                pickle.dump(save_file, open(TRK_filename, 'wb'))

            with open(Stats_filename, 'w') as f:
                json.dump(stats, f, indent=4, cls=CustomJSONEncoder)