#!/usr/bin/env python
"""Implementation of the `GQCNN grasp detection algorithm <https://berkeleyautomation.github.io/gqcnn>`_ on the Franka Emika Panda Robots. This file is
based on the grasp_planner_node.py file that was supplied with the GQCNN package.
"""

## Make script both python2 and python3 compatible ##
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
try:
	input = raw_input
except NameError:
	pass

## Import standard library packages ##
import json
import math
import os
import time
import sys

## Imports third party packages ##
import numpy as np

## Import pyros packages ##
from cv_bridge import CvBridge, CvBridgeError
import rospy

## Import BerkeleyAutomation packages ##
from autolab_core import YamlConfig
from perception import (CameraIntrinsics, ColorImage, DepthImage, BinaryImage,
						RgbdImage)
from visualization import Visualizer2D as vis
from gqcnn.grasping import (Grasp2D, SuctionPoint2D,
							CrossEntropyRobustGraspingPolicy, RgbdImageState, FullyConvolutionalGraspingPolicyParallelJaw, FullyConvolutionalGraspingPolicySuction)
from gqcnn.utils import GripperMode, NoValidGraspsException

from geometry_msgs.msg import PoseStamped
from std_msgs.msg import Header
from gqcnn.srv import (GQCNNGraspPlanner, GQCNNGraspPlannerBoundingBox,
					   GQCNNGraspPlannerSegmask)
from gqcnn.msg import GQCNNGrasp

## Custom imports ##
from panda_autograsp.functions import download_model
from panda_autograsp import Logger
# from panda_autograsp.grasp_planners.gqcnn_ros_grasp_planner import GraspPlanner

#################################################
## Script parameters ############################
#################################################

## Read panda_autograsp configuration file ##
main_cfg = YamlConfig(os.path.abspath(os.path.join(os.path.dirname(
	os.path.realpath(__file__)), "../cfg/main_config.yaml")))

## Get settings out of main_cfg ##
DEFAULT_SOLUTION = main_cfg["defaults"]["solution"]
DEFAULT_MODEL = main_cfg["grasp_detection_solutions"][DEFAULT_SOLUTION]["defaults"]["model"]
MODELS_PATH = os.path.abspath(os.path.join(
	os.path.dirname(os.path.realpath(__file__)), "..", main_cfg["defaults"]["models_dir"]))
DOWNLOAD_SCRIPT_PATH = os.path.abspath(os.path.join(os.path.dirname(
	os.path.realpath(__file__)), "../..", "gqcnn/scripts/downloads/models/download_models.sh"))

#################################################
## Grasp Planner Module #########################
#################################################
class GraspPlanner(object):
	def __init__(self, cfg, cv_bridge, grasping_policy, grasp_pose_publisher):
		"""
		Parameters
		----------
		cfg : dict
			Dictionary of configuration parameters.
		cv_bridge: :obj:`CvBridge`
			ROS `CvBridge`.
		grasping_policy: :obj:`GraspingPolicy`
			Grasping policy to use.
		grasp_pose_publisher: :obj:`Publisher`
			ROS publisher to publish pose of planned grasp for visualization.
		"""
		self.cfg = cfg
		self.cv_bridge = cv_bridge
		self.grasping_policy = grasping_policy
		self.grasp_pose_publisher = grasp_pose_publisher

		# Set minimum input dimensions.
		self.pad = max(
			math.ceil(
				np.sqrt(2) *
				(float(self.cfg["policy"]["metric"]["crop_width"]) / 2)),
			math.ceil(
				np.sqrt(2) *
				(float(self.cfg["policy"]["metric"]["crop_height"]) / 2)))
		self.min_width = 2 * self.pad + self.cfg["policy"]["metric"][
			"crop_width"]
		self.min_height = 2 * self.pad + self.cfg["policy"]["metric"][
			"crop_height"]

	def read_images(self, req):
		"""Reads images from a ROS service request.

		Parameters
		---------
		req: :obj:`ROS ServiceRequest`
			ROS ServiceRequest for grasp planner service.
		"""
		# Get the raw depth and color images as ROS `Image` objects.
		raw_color = req.color_image
		raw_depth = req.depth_image

		# Get the raw camera info as ROS `CameraInfo`.
		raw_camera_info = req.camera_info

		# Wrap the camera info in a BerkeleyAutomation/perception
		# `CameraIntrinsics` object.
		camera_intr = CameraIntrinsics(
			raw_camera_info.header.frame_id, raw_camera_info.K[0],
			raw_camera_info.K[4], raw_camera_info.K[2], raw_camera_info.K[5],
			raw_camera_info.K[1], raw_camera_info.height,
			raw_camera_info.width)

		# Create wrapped BerkeleyAutomation/perception RGB and depth images by
		# unpacking the ROS images using ROS `CvBridge`
		try:
			color_im = ColorImage(self.cv_bridge.imgmsg_to_cv2(
				raw_color, "rgb8"),
								  frame=camera_intr.frame)
			depth_im = DepthImage(self.cv_bridge.imgmsg_to_cv2(
				raw_depth, desired_encoding="passthrough"),
								  frame=camera_intr.frame)
		except CvBridgeError as cv_bridge_exception:
			rospy.logerr(cv_bridge_exception)

		# Check image sizes.
		if color_im.height != depth_im.height or \
		   color_im.width != depth_im.width:
			msg = ("Color image and depth image must be the same shape! Color"
				   " is %d x %d but depth is %d x %d") % (
					   color_im.height, color_im.width, depth_im.height,
					   depth_im.width)
			rospy.logerr(msg)
			raise rospy.ServiceException(msg)

		if (color_im.height < self.min_height
				or color_im.width < self.min_width):
			msg = ("Color image is too small! Must be at least %d x %d"
				   " resolution but the requested image is only %d x %d") % (
					   self.min_height, self.min_width, color_im.height,
					   color_im.width)
			rospy.logerr(msg)
			raise rospy.ServiceException(msg)

		return color_im, depth_im, camera_intr

	def plan_grasp(self, req):
		"""Grasp planner request handler.

		Parameters
		---------
		req: :obj:`ROS ServiceRequest`
			ROS `ServiceRequest` for grasp planner service.
		"""
		color_im, depth_im, camera_intr = self.read_images(req)
		return self._plan_grasp(color_im, depth_im, camera_intr)

	def plan_grasp_bb(self, req):
		"""Grasp planner request handler.

		Parameters
		---------
		req: :obj:`ROS ServiceRequest`
			`ROS ServiceRequest` for grasp planner service.
		"""
		color_im, depth_im, camera_intr = self.read_images(req)
		return self._plan_grasp(color_im,
								depth_im,
								camera_intr,
								bounding_box=req.bounding_box)

	def plan_grasp_segmask(self, req):
		"""Grasp planner request handler.

		Parameters
		---------
		req: :obj:`ROS ServiceRequest`
			ROS `ServiceRequest` for grasp planner service.
		"""
		color_im, depth_im, camera_intr = self.read_images(req)
		raw_segmask = req.segmask
		try:
			segmask = BinaryImage(self.cv_bridge.imgmsg_to_cv2(
				raw_segmask, desired_encoding="passthrough"),
								  frame=camera_intr.frame)
		except CvBridgeError as cv_bridge_exception:
			rospy.logerr(cv_bridge_exception)
		if color_im.height != segmask.height or \
		   color_im.width != segmask.width:
			msg = ("Images and segmask must be the same shape! Color image is"
				   " %d x %d but segmask is %d x %d") % (
					   color_im.height, color_im.width, segmask.height,
					   segmask.width)
			rospy.logerr(msg)
			raise rospy.ServiceException(msg)

		return self._plan_grasp(color_im,
								depth_im,
								camera_intr,
								segmask=segmask)

	def _plan_grasp(self,
					color_im,
					depth_im,
					camera_intr,
					bounding_box=None,
					segmask=None):
		"""Grasp planner request handler.

		Parameters
		---------
		req: :obj:`ROS ServiceRequest`
			ROS `ServiceRequest` for grasp planner service.
		"""
		rospy.loginfo("Planning Grasp")

		# Inpaint images.
		color_im = color_im.inpaint(
			rescale_factor=self.cfg["inpaint_rescale_factor"])
		depth_im = depth_im.inpaint(
			rescale_factor=self.cfg["inpaint_rescale_factor"])

		# Init segmask.
		if segmask is None:
			segmask = BinaryImage(255 *
								  np.ones(depth_im.shape).astype(np.uint8),
								  frame=color_im.frame)

		# Visualize.
		if self.cfg["vis"]["color_image"]:
			vis.imshow(color_im)
			vis.show()
		if self.cfg["vis"]["depth_image"]:
			vis.imshow(depth_im)
			vis.show()
		if self.cfg["vis"]["segmask"] and segmask is not None:
			vis.imshow(segmask)
			vis.show()

		# Aggregate color and depth images into a single
		# BerkeleyAutomation/perception `RgbdImage`.
		rgbd_im = RgbdImage.from_color_and_depth(color_im, depth_im)

		# Mask bounding box.
		if bounding_box is not None:
			# Calc bb parameters.
			min_x = bounding_box.minX
			min_y = bounding_box.minY
			max_x = bounding_box.maxX
			max_y = bounding_box.maxY

			# Contain box to image->don't let it exceed image height/width
			# bounds.
			if min_x < 0:
				min_x = 0
			if min_y < 0:
				min_y = 0
			if max_x > rgbd_im.width:
				max_x = rgbd_im.width
			if max_y > rgbd_im.height:
				max_y = rgbd_im.height

			# Mask.
			bb_segmask_arr = np.zeros([rgbd_im.height, rgbd_im.width])
			bb_segmask_arr[min_y:max_y, min_x:max_x] = 255
			bb_segmask = BinaryImage(bb_segmask_arr.astype(np.uint8),
									 segmask.frame)
			segmask = segmask.mask_binary(bb_segmask)

		# Visualize.
		if self.cfg["vis"]["rgbd_state"]:
			masked_rgbd_im = rgbd_im.mask_binary(segmask)
			vis.figure()
			vis.subplot(1, 2, 1)
			vis.imshow(masked_rgbd_im.color)
			vis.subplot(1, 2, 2)
			vis.imshow(masked_rgbd_im.depth)
			vis.show()

		# Create an `RgbdImageState` with the cropped `RgbdImage` and
		# `CameraIntrinsics`.
		rgbd_state = RgbdImageState(rgbd_im, camera_intr, segmask=segmask)

		# Execute policy.
		try:
			return self.execute_policy(rgbd_state, self.grasping_policy,
									   self.grasp_pose_publisher,
									   camera_intr.frame)
		except NoValidGraspsException:
			rospy.logerr(
				("While executing policy found no valid grasps from sampled"
				 " antipodal point pairs. Aborting Policy!"))
			raise rospy.ServiceException(
				("While executing policy found no valid grasps from sampled"
				 " antipodal point pairs. Aborting Policy!"))

	def execute_policy(self, rgbd_image_state, grasping_policy,
					   grasp_pose_publisher, pose_frame):
		"""Executes a grasping policy on an `RgbdImageState`.

		Parameters
		----------
		rgbd_image_state: :obj:`RgbdImageState`
			`RgbdImageState` from BerkeleyAutomation/perception to encapsulate
			depth and color image along with camera intrinsics.
		grasping_policy: :obj:`GraspingPolicy`
			Grasping policy to use.
		grasp_pose_publisher: :obj:`Publisher`
			ROS publisher to publish pose of planned grasp for visualization.
		pose_frame: :obj:`str`
			Frame of reference to publish pose in.
		"""
		# Execute the policy"s action.
		grasp_planning_start_time = time.time()
		grasp = grasping_policy(rgbd_image_state)

		# Create `GQCNNGrasp` return msg and populate it.
		gqcnn_grasp = GQCNNGrasp()
		gqcnn_grasp.q_value = grasp.q_value
		gqcnn_grasp.pose = grasp.grasp.pose().pose_msg
		if isinstance(grasp.grasp, Grasp2D):
			gqcnn_grasp.grasp_type = GQCNNGrasp.PARALLEL_JAW
		elif isinstance(grasp.grasp, SuctionPoint2D):
			gqcnn_grasp.grasp_type = GQCNNGrasp.SUCTION
		else:
			rospy.logerr("Grasp type not supported!")
			raise rospy.ServiceException("Grasp type not supported!")

		# Store grasp representation in image space.
		gqcnn_grasp.center_px[0] = grasp.grasp.center[0]
		gqcnn_grasp.center_px[1] = grasp.grasp.center[1]
		gqcnn_grasp.angle = grasp.grasp.angle
		gqcnn_grasp.depth = grasp.grasp.depth
		gqcnn_grasp.thumbnail = grasp.image.rosmsg

		# Create and publish the pose alone for easy visualization of grasp
		# pose in Rviz.
		pose_stamped = PoseStamped()
		pose_stamped.pose = grasp.grasp.pose().pose_msg
		header = Header()
		header.stamp = rospy.Time.now()
		header.frame_id = pose_frame
		pose_stamped.header = header
		grasp_pose_publisher.publish(pose_stamped)

		# Return `GQCNNGrasp` msg.
		rospy.loginfo("Total grasp planning time: " +
					  str(time.time() - grasp_planning_start_time) + " secs.")

		# Visualize result
		if self.cfg["vis"]["final_grasp"]:
			vis.figure(size=(10, 10))
			vis.imshow(rgbd_image_state.rgbd_im.color,
					   vmin=self.cfg["policy"]["vis"]["vmin"],
					   vmax=self.cfg["policy"]["vis"]["vmax"])
			vis.grasp(grasp.grasp, scale=2.5,
					  show_center=False, show_axis=True)
			vis.title("Planned grasp at depth {0:.3f}m with Q={1:.3f}".format(
				grasp.grasp.depth, grasp.q_value))
			vis.show()

		## Return grasp ##
		return gqcnn_grasp

#################################################
## Main script ##################################
#################################################
if __name__ == "__main__":

	## Initialize the ROS node. ##
	rospy.init_node("grasp_planner_server")

	## Initialize `CvBridge`. ##
	cv_bridge = CvBridge()

	## Argument parser ##
	try:
		model_name = rospy.get_param("~model_name")
	except KeyError:
		model_name = DEFAULT_MODEL
	try:
		model_dir = rospy.get_param("~model_dir")
		if model_dir == "default":
			model_dir = os.path.abspath(os.path.join(MODELS_PATH, model_name))
	except KeyError:
		model_dir = os.path.abspath(os.path.join(MODELS_PATH, model_name))

	## Download CNN model if not present ##
	model_dir = os.path.join(MODELS_PATH, model_name)
	if not os.path.exists(model_dir):
		rospy.logwarn("The " + model_name + " model was not found in the models folder. This model is required to continue.")
		while True:
			prompt_result = raw_input(
				"Do you want to download this model now? [Y/n] ")
			# Check user input #
			# If yes download sample
			if prompt_result.lower() in ['y', 'yes']:
				val = download_model(model_name, MODELS_PATH, DOWNLOAD_SCRIPT_PATH)
				if not val == 0: # Check if download was successful
					shutdown_msg = "Shutting down %s node because grasp model could not downloaded." % (
					model_name)
					rospy.logwarn(shutdown_msg)
					sys.exit(0)
				else:
					break
			elif prompt_result.lower() in ['n', 'no']:
				shutdown_msg = "Shutting down %s node because grasp model is not downloaded." % (
				model_name)
				rospy.logwarn(shutdown_msg)
				sys.exit(0)
			elif prompt_result == "":
				download_model(model_name, MODELS_PATH, DOWNLOAD_SCRIPT_PATH)
				if not val == 0: # Check if download was successful
					shutdown_msg = "Shutting down %s node because grasp model could not downloaded." % (
					model_name)
					rospy.logwarn(shutdown_msg)
					sys.exit(0)
				else:
					break
			else:
				print(
					prompt_result + " is not a valid response please answer with Y or N to continue.")

	## Retrieve model related configuration values ##
	model_config = json.load(
		open(os.path.join(model_dir, "config.json"), "r"))
	try:
		gqcnn_config = model_config["gqcnn"]
		gripper_mode = gqcnn_config["gripper_mode"]
	except KeyError:
		gqcnn_config = model_config["gqcnn_config"]
		input_data_mode = gqcnn_config["input_data_mode"]
		if input_data_mode == "tf_image":
			gripper_mode = GripperMode.LEGACY_PARALLEL_JAW
		elif input_data_mode == "tf_image_suction":
			gripper_mode = GripperMode.LEGACY_SUCTION
		elif input_data_mode == "suction":
			gripper_mode = GripperMode.SUCTION
		elif input_data_mode == "multi_suction":
			gripper_mode = GripperMode.MULTI_SUCTION
		elif input_data_mode == "parallel_jaw":
			gripper_mode = GripperMode.PARALLEL_JAW
		else:
			raise ValueError(
				"Input data mode {} not supported!".format(input_data_mode))

	## Get the policy based config parameters ##
	config_filename = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)),
												   "../..", "gqcnn/cfg/examples/gqcnn_suction.yaml"))
	if (gripper_mode == GripperMode.LEGACY_PARALLEL_JAW
			or gripper_mode == GripperMode.PARALLEL_JAW):
		config_filename = os.path.abspath(os.path.join(
			os.path.dirname(os.path.realpath(__file__)), "../..",
			"gqcnn/cfg/examples/gqcnn_pj.yaml"))

	## Get CNN and Policy files ##
	cfg = YamlConfig(config_filename)
	policy_cfg = cfg["policy"]
	policy_cfg["metric"]["gqcnn_model"] = model_dir

	## Add main Polyicy values to the GQCNN based cfg ##
	# This allows us to add and overwrite to the original GQCNN config file
	cfg.update(main_cfg)

	## Create publisher to publish pose of final grasp ##
	grasp_pose_publisher = rospy.Publisher("/gqcnn_grasp/pose",
										   PoseStamped,
										   queue_size=10)

	## Create grasping policy ##
	rospy.loginfo("Creating Grasping Policy")
	try:
		if "cross_entropy" == main_cfg["grasp_detection_solutions"]["gqcnn"]["parameters"]["available"][model_name]:
			grasping_policy = CrossEntropyRobustGraspingPolicy(policy_cfg)
		elif "fully_conv" == main_cfg["grasp_detection_solutions"]["gqcnn"]["parameters"]["available"][model_name]:
			if "pj" in model_name.lower():
				grasping_policy = FullyConvolutionalGraspingPolicyParallelJaw(policy_cfg)
			elif "suction" in model_name.lower():
				grasping_policy = FullyConvolutionalGraspingPolicySuction(policy_cfg)
	except KeyError:
		rospy.loginfo("The %s model of the %s policy is not yet implemented." % (model_name, "gqcnn"))
		sys.exit(0)

	## Create a grasp planner ##
	grasp_planner = GraspPlanner(cfg, cv_bridge, grasping_policy,
								 grasp_pose_publisher)

	## Initialize the ROS services ##
	grasp_planning_service = rospy.Service("grasp_planner", GQCNNGraspPlanner,
										   grasp_planner.plan_grasp)
	grasp_planning_service_bb = rospy.Service("grasp_planner_bounding_box",
											  GQCNNGraspPlannerBoundingBox,
											  grasp_planner.plan_grasp_bb)
	grasp_planning_service_segmask = rospy.Service(
		"grasp_planner_segmask", GQCNNGraspPlannerSegmask,
		grasp_planner.plan_grasp_segmask)
	rospy.loginfo("Grasping Policy Initialized")

	## Spin forever. ##
	rospy.spin()