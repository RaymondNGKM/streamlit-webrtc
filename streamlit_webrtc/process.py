import abc
import itertools
import logging
import queue
import sys
import threading
import traceback
from typing import Optional, Union

import av
import numpy as np
from aiortc import MediaStreamTrack

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class VideoTransformerBase(abc.ABC):
    @abc.abstractmethod
    def transform(self, frame: av.VideoFrame) -> np.ndarray:
        """ Backward compatibility; Returns a new video frame in bgr24 format """


class VideoProcessorBase(abc.ABC):
    @abc.abstractmethod
    def recv(self, frame: av.VideoFrame) -> np.ndarray:
        """ Processes the received frame and returns a frame in bgr24 format """


VideoProcessor = Union[
    VideoProcessorBase, VideoTransformerBase
]  # Backward compatibility


class VideoProcessTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, track: MediaStreamTrack, video_processor: VideoProcessorBase):
        super().__init__()  # don't forget this!
        self.track = track
        self.processor = video_processor

    async def recv(self):
        frame = await self.track.recv()

        # XXX: Backward compatibility
        if hasattr(self.processor, "recv"):
            img = self.processor.recv(frame)
        else:
            logger.warning(".transform() is deprecated. Use .recv() instead.")
            img = self.processor.transform(frame)

        # rebuild a av.VideoFrame, preserving timing information
        new_frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        new_frame.pts = frame.pts
        new_frame.time_base = frame.time_base
        return new_frame


__SENTINEL__ = "__SENTINEL__"

# See https://stackoverflow.com/a/42007659
video_processing_thread_id_generator = itertools.count()


class AsyncVideoProcessTrack(MediaStreamTrack):
    kind = "video"

    _in_queue: queue.Queue

    def __init__(
        self,
        track: MediaStreamTrack,
        video_processor: VideoProcessorBase,
        stop_timeout: Optional[float] = None,
    ):
        super().__init__()  # don't forget this!
        self.track = track
        self.processor = video_processor

        self._thread = threading.Thread(
            target=self._run_worker_thread,
            name=f"async_video_processor_{next(video_processing_thread_id_generator)}",
        )
        self._in_queue = queue.Queue()
        self._latest_result_img_lock = threading.Lock()

        self._busy = False
        self._latest_result_img: Union[np.ndarray, None] = None

        self._thread.start()

        self.stop_timeout = stop_timeout

    def _run_worker_thread(self):
        try:
            self._worker_thread()
        except Exception:
            logger.error("Error occurred in the WebRTC thread:")

            exc_type, exc_value, exc_traceback = sys.exc_info()
            for tb in traceback.format_exception(exc_type, exc_value, exc_traceback):
                for tbline in tb.rstrip().splitlines():
                    logger.error(tbline.rstrip())

    def _worker_thread(self):
        while True:
            item = self._in_queue.get()
            if item == __SENTINEL__:
                break

            stop_requested = False
            while not self._in_queue.empty():
                item = self._in_queue.get_nowait()
                if item == __SENTINEL__:
                    stop_requested = True
            if stop_requested:
                break

            if item is None:
                raise Exception("A queued item is unexpectedly None")

            # XXX: Backward compatibility
            if hasattr(self.processor, "recv"):
                result_img = self.processor.recv(item)
            else:
                logger.warning(".transform() is deprecated. Use .recv() instead.")
                result_img = self.processor.transform(item)

            with self._latest_result_img_lock:
                self._latest_result_img = result_img

    def stop(self):
        self._in_queue.put(__SENTINEL__)
        self._thread.join(self.stop_timeout)

        return super().stop()

    async def recv(self):
        frame = await self.track.recv()
        self._in_queue.put(frame)

        with self._latest_result_img_lock:
            if self._latest_result_img is not None:
                # rebuild a av.VideoFrame, preserving timing information
                new_frame = av.VideoFrame.from_ndarray(
                    self._latest_result_img, format="bgr24"
                )
                new_frame.pts = frame.pts
                new_frame.time_base = frame.time_base
                return new_frame
            else:
                return frame