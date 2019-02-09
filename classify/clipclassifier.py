import json
import logging

from datetime import datetime, timedelta
import numpy as np
import os.path
from PIL import Image, ImageDraw, ImageFont
import time
from typing import Dict

from classify.trackprediction import TrackPrediction
import classify.globals as globs
from ml_tools import tools
from ml_tools.cptvfileprocessor import CPTVFileProcessor
from ml_tools.dataset import Preprocessor
from ml_tools.model import Model
from ml_tools.mpeg_creator import MPEGCreator
from track.track import Track
from track.trackextractor import TrackExtractor
from track.region import Region


HERE = os.path.dirname(__file__)
RESOURCES_PATH = os.path.join(HERE, "resources")

def resource_path(name):
    return os.path.join(RESOURCES_PATH, name)

class ClipClassifier(CPTVFileProcessor):
    """ Classifies tracks within CPTV files. """

    # skips every nth frame.  Speeds things up a little, but reduces prediction quality.
    FRAME_SKIP = 1

    def __init__(self, config, tracking_config):
        """ Create an instance of a clip classifier"""

        super(ClipClassifier, self).__init__(config, tracking_config)

        # prediction record for each track
        self.track_prediction: Dict[Track, TrackPrediction] = {}

        # mpeg preview output
        self.enable_previews = False
        self.enable_side_by_side = False

        self.start_date = None
        self.end_date = None

        # enables exports detailed information for each track.  If preview mode is enabled also enables track previews.
        self.enable_per_track_information = False

        # includes both original, and predicted tag in filename
        self.include_prediction_in_filename = False

        # writes metadata to standard out instead of a file.
        self.write_meta_to_stdout = False

    @property
    def font(self):
        """ gets default font. """
        if not globs._classifier_font: globs._classifier_font = ImageFont.truetype(resource_path("Ubuntu-R.ttf"), 12)
        return globs._classifier_font

    @property
    def font_title(self):
        """ gets default title font. """
        if not globs._classifier_font_title: globs._classifier_font_title = ImageFont.truetype(resource_path("Ubuntu-B.ttf"), 14)
        return globs._classifier_font_title

    def preprocess(self, frame, thermal_reference):
        """
        Applies preprocessing to frame required by the model.
        :param frame: numpy array of shape [C, H, W]
        :return: preprocessed numpy array
        """

        # note, would be much better if the model did this, as only the model knows how preprocessing occured during
        # training
        frame = np.float32(frame)
        frame[2:3+1] *= (1 / 256)
        frame[0] -= thermal_reference

        return frame

    def identify_track(self, tracker:TrackExtractor, track: Track):
        """
        Runs through track identifying segments, and then returns it's prediction of what kind of animal this is.
        One prediction will be made for every frame.
        :param track: the track to identify.
        :return: TrackPrediction object
        """

        # uniform prior stats start with uniform distribution.  This is the safest bet, but means that
        # it takes a while to make predictions.  When off the first prediction is used instead causing
        # faster, but potentially more unstable predictions.
        UNIFORM_PRIOR = False

        predictions = []
        novelties = []

        num_labels = len(self.classifier.labels)
        prediction_smooth = 0.1

        smooth_prediction = None
        smooth_novelty = None

        prediction = 0.0
        novelty = 0.0

        fp_index = self.classifier.labels.index('false-positive')

        # go through making clas sifications at each frame
        # note: we should probably be doing this every 9 frames or so.
        state = None
        for i in range(len(track)):

            # note: would be much better for the tracker to store the thermal references as it goes.
            thermal_reference = np.median(tracker.frame_buffer.thermal[track.start_frame + i])

            frame = tracker.get_track_channels(track, i)
            if i % self.FRAME_SKIP == 0:

                # we use a tigher cropping here so we disable the default 2 pixel inset
                frame = Preprocessor.apply([frame], [thermal_reference], default_inset=0)[0]

                prediction, novelty, state = self.classifier.classify_frame_with_novelty(frame, state)

                # make false-positive prediction less strong so if track has dead footage it won't dominate a strong
                # score
                prediction[fp_index] *= 0.8

                # a little weight decay helps the model not lock into an initial impression.
                # 0.98 represents a half life of around 3 seconds.
                state *= 0.98

                # precondition on weight,  segments with small mass are weighted less as we can assume the error is
                # higher here.
                mass = track.bounds_history[i].mass

                # we use the square-root here as the mass is in units squared.
                # this effectively means we are giving weight based on the diameter
                # of the object rather than the mass.
                mass_weight = np.clip(mass / 20, 0.02, 1.0) ** 0.5

                # cropped frames don't do so well so restrict their score
                cropped_weight = 0.7 if track.bounds_history[i].was_cropped else 1.0

                prediction *= mass_weight * cropped_weight

            else:
                # just continue prediction and state along.
                pass

            if smooth_prediction is None:
                if UNIFORM_PRIOR:
                    smooth_prediction = np.ones([num_labels]) * (1 / num_labels)
                else:
                    smooth_prediction = prediction
                smooth_novelty = 0.5
            else:
                smooth_prediction = (1-prediction_smooth) * smooth_prediction + prediction_smooth * prediction
                smooth_novelty = (1-prediction_smooth) * smooth_novelty + prediction_smooth * novelty

            predictions.append(smooth_prediction)
            novelties.append(smooth_novelty)

        return TrackPrediction(predictions, novelties)

    @property
    def classifier(self):
        """
        Returns a classifier object, which is created on demand.
        This means if the ClipClassifier is copied to a new process a new Classifier instance will be created.
        """
        print("loading classifier")
        if globs._classifier is None:
            t0 = datetime.now()
            logging.info("classifier loading")
            globs._classifier = Model(tools.get_session(disable_gpu=not self.config.use_gpu))
            globs._classifier.load(self.config.classify.model)
            logging.info("classifier loaded ({})".format(datetime.now() - t0))

        return globs._classifier

    def get_clip_prediction(self):
        """ Returns list of class predictions for all tracks in this clip. """

        class_best_score = [0 for _ in range(len(self.classifier.labels))]

        # keep track of our highest confidence over every track for each class
        for _, prediction in self.track_prediction.items():
            for i in range(len(self.classifier.labels)):
                class_best_score[i] = max(class_best_score[i], prediction.class_best_score[i])

        results = []
        for n in range(1, 1+len(self.classifier.labels)):
            nth_label = int(np.argsort(class_best_score)[-n])
            nth_score = float(np.sort(class_best_score)[-n])
            results.append((self.classifier.labels[nth_label], nth_score))

        return results

    def fit_to_screen(self, rect:Region, screen_bounds:Region):
        """ Modifies rect so that rect is visible within bounds. """
        if rect.left < screen_bounds.left:
            rect.x = screen_bounds.left
        if rect.top < screen_bounds.top:
            rect.y = screen_bounds.top

        if rect.right > screen_bounds.right:
            rect.x = screen_bounds.right - rect.width

        if rect.bottom > screen_bounds.bottom:
            rect.y = screen_bounds.bottom - rect.height

    def export_clip_preview(self, filename, tracker:TrackExtractor):
        """
        Exports a clip showing the tracking and predictions for objects within the clip.
        """

        # increased resolution of video file.
        # videos look much better scaled up
        FRAME_SCALE = 4.0

        NORMALISATION_SMOOTH = 0.95

        # amount pad at ends of thermal range
        HEAD_ROOM = 25

        auto_min = np.min(tracker.frame_buffer.thermal[0])
        auto_max = np.max(tracker.frame_buffer.thermal[0])

        # setting quality to 30 gives files approximately the same size as the original CPTV MPEG previews
        # (but they look quite compressed)
        mpeg = MPEGCreator(filename)

        for frame_number, thermal in enumerate(tracker.frame_buffer.thermal):

            thermal_min = np.min(thermal)
            thermal_max = np.max(thermal)

            auto_min = NORMALISATION_SMOOTH * auto_min + (1 - NORMALISATION_SMOOTH) * (thermal_min-HEAD_ROOM)
            auto_max = NORMALISATION_SMOOTH * auto_max + (1 - NORMALISATION_SMOOTH) * (thermal_max+HEAD_ROOM)

            # sometimes we get an extreme value that throws off the autonormalisation, so if there are values outside
            # of the expected range just instantly switch levels
            if thermal_min < auto_min or thermal_max > auto_max:
                auto_min = thermal_min
                auto_max = thermal_max

            thermal_image = tools.convert_heat_to_img(thermal, self.colormap, auto_min, auto_max)
            thermal_image = thermal_image.resize((int(thermal_image.width * FRAME_SCALE), int(thermal_image.height * FRAME_SCALE)), Image.BILINEAR)

            if tracker.frame_buffer.filtered:
                if self.enable_side_by_side:
                    # put thermal & tracking images side by side
                    tracking_image = self.export_tracking_frame(tracker, frame_number, FRAME_SCALE)
                    side_by_side_image = Image.new('RGB', (tracking_image.width * 2, tracking_image.height))
                    side_by_side_image.paste(thermal_image, (0, 0))
                    side_by_side_image.paste(tracking_image, (tracking_image.width, 0))
                    mpeg.next_frame(np.asarray(side_by_side_image))
                else:
                    # overlay track rectanges on original thermal image
                    thermal_image = self.draw_track_rectangles(tracker, frame_number, FRAME_SCALE, thermal_image)
                    mpeg.next_frame(np.asarray(thermal_image))

            else:
                # no filtered frames available (clip too hot or
                # background moving?) so just output the original
                # frame without the tracking frame.
                mpeg.next_frame(np.asarray(thermal_image))

            # we store the entire video in memory so we need to cap the frame count at some point.
            if frame_number > 9 * 60 * 10:
                break

        mpeg.close()

    def export_tracking_frame(self, tracker: TrackExtractor, frame_number:int, frame_scale:float):

        mask = tracker.frame_buffer.mask[frame_number]

        filtered = tracker.frame_buffer.filtered[frame_number]
        tracking_image = tools.convert_heat_to_img(filtered / 200, self.colormap, temp_min=0, temp_max=1)

        tracking_image = tracking_image.resize((int(tracking_image.width * frame_scale), int(tracking_image.height * frame_scale)), Image.NEAREST)

        return self.draw_track_rectangles(tracker, frame_number, frame_scale, tracking_image)

    def draw_track_rectangles(self, tracker, frame_number, frame_scale, image):
        draw = ImageDraw.Draw(image)

        # look for any tracks that occur on this frame
        for _, track in enumerate(tracker.tracks):

            prediction = self.track_prediction[track]

            # find a track description, which is the final guess of what this class is.
            guesses = ["{} ({:.1f})".format(
                self.classifier.labels[prediction.label(i)], prediction.score(i) * 10) for i in range(1, 4)
                if prediction.score(i) > 0.5]

            track_description = "\n".join(guesses)
            track_description.strip()

            frame_offset = frame_number - track.start_frame
            if 0 < frame_offset < len(track.bounds_history) - 1:
                # display the track
                rect = track.bounds_history[frame_offset].copy()

                rect_points = [int(p * frame_scale) for p in [rect.left, rect.top, rect.right, rect.top, rect.right,
                                                              rect.bottom, rect.left, rect.bottom, rect.left,
                                                              rect.top]]
                draw.line(rect_points, (255, 64, 32))

                if track not in self.track_prediction:
                    # no information for this track just ignore
                    current_prediction_string = ''
                else:
                    label = self.classifier.labels[prediction.label_at_time(frame_offset)]
                    score = prediction.score_at_time(frame_offset)
                    if score >= 0.7:
                        prediction_format = "({:.1f} {})"
                    else:
                        prediction_format = "({:.1f} {})?"
                    current_prediction_string = prediction_format.format(score * 10, label)

                    current_prediction_string += "\nnovelty={:.2f}".format(prediction.novelty_history[frame_offset])

                header_size = self.font_title.getsize(track_description)
                footer_size = self.font.getsize(current_prediction_string)

                # figure out where to draw everything
                header_rect = Region(rect.left * frame_scale, rect.top * frame_scale - header_size[1], header_size[0], header_size[1])
                footer_center = ((rect.width * frame_scale) - footer_size[0]) / 2
                footer_rect = Region(rect.left * frame_scale + footer_center, rect.bottom * frame_scale, footer_size[0], footer_size[1])

                screen_bounds = Region(0, 0, image.width, image.height)

                self.fit_to_screen(header_rect, screen_bounds)
                self.fit_to_screen(footer_rect, screen_bounds)

                draw.text((header_rect.x, header_rect.y), track_description, font=self.font_title)
                draw.text((footer_rect.x, footer_rect.y), current_prediction_string, font=self.font)

        return image

    def needs_processing(self, filename):
        """
        Returns True if this file needs to be processed, false otherwise.
        :param filename: the full path and filename of the cptv file in question.
        :return: returns true if file should be processed, false otherwise
        """

        # check date filters
        date_part = str(os.path.basename(filename).split("-")[0])
        date = datetime.strptime(date_part, "%Y%m%d")
        if self.start_date and date < self.start_date:
            return False
        if self.end_date and date > self.end_date:
            return False

        # look to see of the destination file already exists.
        base_name = self.get_base_name(filename)
        meta_filename = base_name + '.txt'

        # if no stats file exists we haven't processed file, so reprocess

        # otherwise check what needs to be done.
        if self.overwrite_mode == self.OM_ALL:
            return True
        elif self.overwrite_mode == self.OM_NONE:
            return not os.path.exists(meta_filename)
        else:
            raise Exception("Overwrite mode {} not supported.".format(self.overwrite_mode))

    def get_meta_data(self, filename):
        """ Reads meta-data for a given cptv file. """
        source_meta_filename = os.path.splitext(filename)[0] + ".txt"
        if os.path.exists(source_meta_filename):

            meta_data = tools.load_clip_metadata(source_meta_filename)

            tags = set()
            for record in meta_data["Tags"]:
                # skip automatic tags
                if record.get("automatic", False):
                    continue
                else:
                    tags.add(record['animal'])

            tags = list(tags)

            if len(tags) == 0:
                tag = 'no tag'
            elif len(tags) == 1:
                tag = tags[0] if tags[0] else "none"
            else:
                print(tags)
                tag = 'multi'
            meta_data["primary_tag"] = tag
            return meta_data
        else:
            return None

    def get_base_name(self, input_filename):
        """ Returns the base path and filename for an output filename from an input filename. """
        if self.include_prediction_in_filename:
            meta_data = self.get_meta_data(input_filename)
            tag_part = '[' + (meta_data["primary_tag"] if meta_data else "none") + '] '
        else:
            tag_part = ''
        return os.path.splitext(os.path.join(self.config.tracks_folder, tag_part + os.path.basename(input_filename)))[0]

    def process_all(self, root):
        for root, folders, files in os.walk(root):
            for folder in folders:
                if folder not in IGNORE_FOLDERS:
                    self.process_folder(os.path.join(root,folder), tag=folder.lower())

    def process_file(self, filename, **kwargs):
        """
        Process a file extracting tracks and identifying them.
        :param filename: filename to process
        :param enable_preview: if true an MPEG preview file is created.
        """

        if not os.path.exists(filename):
            raise Exception("File {} not found.".format(filename))

        start = time.time()

        tracker = TrackExtractor(self.tracker_config)
        tracker.load(filename)

        tracker.extract_tracks()

        if len(tracker.tracks) > 0:
            # optical flow is not generated by default, if we have at least one track we will need to generate it here.
            if not tracker.frame_buffer.has_flow:
                tracker.frame_buffer.generate_flow(tracker.opt_flow)

        base_name = self.get_base_name(filename)
        destination_folder = os.path.dirname(base_name)

        if not os.path.exists(destination_folder):
            logging.info("Creating folder {}".format(destination_folder))
            os.makedirs(destination_folder)

        if self.include_prediction_in_filename:
            mpeg_filename = base_name + "{}" + '.mp4'
        else:
            mpeg_filename = base_name + '.mp4'

        meta_filename = base_name + '.txt'

        # reset track predictions
        self.track_prediction = {}

        logging.info(os.path.basename(filename)+":")

        # identify each track
        for i, track in enumerate(tracker.tracks):

            prediction = self.identify_track(tracker, track)

            self.track_prediction[track] = prediction

            description = prediction.description(self.classifier.labels)

            logging.info(" - [{}/{}] prediction: {}".format(i + 1, len(tracker.tracks), description))

        if self.enable_previews:
            prediction_string = ""
            for label, score in self.get_clip_prediction():
                if score > 0.5:
                    prediction_string = prediction_string + " {} {:.1f}".format(label, score * 10)
            self.export_clip_preview(mpeg_filename.format(prediction_string), tracker)

        # record results in text file.
        save_file = {}
        save_file['source'] = filename
        save_file['start_time'] = tracker.video_start_time.isoformat()
        save_file['end_time'] = (tracker.video_start_time + timedelta(seconds=len(tracker.frame_buffer.thermal) / 9.0)).isoformat()

        # read in original metadata
        meta_data = self.get_meta_data(filename)

        if meta_data:
            save_file['camera'] = meta_data['Device']['devicename']
            save_file['cptv_meta'] = meta_data
            save_file['original_tag'] = meta_data['primary_tag']
        save_file['tracks'] = []
        for track, prediction in self.track_prediction.items():
            track_info = {}
            save_file['tracks'].append(track_info)
            track_info['start_time'] = track.start_time.isoformat()
            track_info['end_time'] = track.end_time.isoformat()
            track_info['num_frames'] = prediction.num_frames
            track_info['frame_start'] = track.start_frame
            track_info['label'] = self.classifier.labels[prediction.label()]
            track_info['confidence'] = round(prediction.score(), 2)
            track_info['clarity'] = round(prediction.clarity, 3)
            track_info['average_novelty'] = round(prediction.average_novelty, 2)
            track_info['max_novelty'] = round(prediction.max_novelty, 2)
            track_info['all_class_confidences'] = {}
            for i, value in enumerate(prediction.class_best_score):
                label = self.classifier.labels[i]
                track_info['all_class_confidences'][label] = round(value, 3)


        if self.write_meta_to_stdout:
            output = json.dumps(save_file, indent=4, cls=tools.CustomJSONEncoder)
            print(output)
        else:
            f = open(meta_filename, 'w')
            json.dump(save_file, f, indent=4, cls=tools.CustomJSONEncoder)

        ms_per_frame = (time.time() - start) * 1000 / max(1, len(tracker.frame_buffer.thermal))
        if self.verbose:
            logging.info("Took {:.1f}ms per frame".format(ms_per_frame))
