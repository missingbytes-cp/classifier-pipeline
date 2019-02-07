# We store some cached shared objects as globals as they can not be passed around processes, and therefore would
# break the worker threads system.  Instead we load them on demand and store them in each processors global space.
_classifier = None
_classifier_font = None
_classifier_font_title = None

