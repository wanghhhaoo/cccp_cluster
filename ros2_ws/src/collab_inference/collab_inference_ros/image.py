import numpy as np
from cv_bridge import CvBridge


class ImageDecoder:
    """Convert ROS images to the RGB uint8 arrays expected by both models."""

    def __init__(self):
        self._bridge = CvBridge()

    def rgb8(self, message):
        image = self._bridge.imgmsg_to_cv2(message, desired_encoding="rgb8")
        image = np.asarray(image)
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("image must have three RGB channels")
        if image.dtype != np.uint8:
            raise ValueError("image must decode to uint8 RGB")
        return np.ascontiguousarray(image)
