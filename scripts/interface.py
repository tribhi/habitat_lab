#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import rospy
from rospy.numpy_msg import numpy_msg
from rospy_tutorials.msg import Floats
from geometry_msgs.msg import Twist
from geometry_msgs.msg import PoseStamped
import threading
import sys
import tf
from habitat.utils.geometry_utils import quaternion_rotate_vector
from habitat.tasks.utils import cartesian_to_polar
sys.path = [
    b for b in sys.path if "2.7" not in b
]  # remove path's related to ROS from environment or else certain packages like cv2 can't be imported

import habitat
import habitat_sim.bindings as hsim
import magnum as mn
import numpy as np
import time
import cv2

lock = threading.Lock()
rospy.init_node("habitat", anonymous=False)

def convert_points_to_topdown(pathfinder, points, meters_per_pixel = 0.5):
    points_topdown = []
    bounds = pathfinder.get_bounds()
    for point in points:
        # convert 3D x,z to topdown x,y
        px = (point[0] - bounds[0][0]) / meters_per_pixel
        py = (point[2] - bounds[0][2]) / meters_per_pixel
        points_topdown.append(np.array([px, py]))
    return points_topdown

def quat_to_coeff(quat):
        quaternion = [quat.x, quat.y, quat.z, quat.w]
        return quaternion

class sim_env(threading.Thread):

    _x_axis = 0
    _y_axis = 1
    _z_axis = 2
    _dt = 0.00478
    _sensor_rate = 50  # hz
    _r = rospy.Rate(_sensor_rate)

    def __init__(self, env_config_file):
        threading.Thread.__init__(self)
        self.env = habitat.Env(config=habitat.get_config(env_config_file))
        # always assume height equals width
        
        self.env._sim.agents[0].move_filter_fn = self.env._sim.step_filter
        self.observations = self.env.reset()

        self.env._sim.agents[0].state.velocity = np.float32([0, 0, 0])
        self.env._sim.agents[0].state.angular_velocity = np.float32([0, 0, 0])
        # self._sensor_resolution = {
        #     "RGB": self.env._sim.config["RGB_SENSOR"]["HEIGHT"],
        #     "DEPTH": self.env._sim.config["DEPTH_SENSOR"]["HEIGHT"],
        #     "BC_SENSOR": self.env._sim.config["BC_SENSOR"]["HEIGHT"],
        # }
        config = self.env._sim.config
        print(self.env._sim.active_dataset)
        self._sensor_resolution = {
            "RGB": 256,
            "DEPTH": 256,
        }
        print(self.env._sim.pathfinder.get_bounds())
        self._pub_rgb = rospy.Publisher("~rgb", numpy_msg(Floats), queue_size=1)
        self._pub_depth = rospy.Publisher("~depth", numpy_msg(Floats), queue_size=1)
        self._pub_pose = rospy.Publisher("~pose", PoseStamped, queue_size=1)

        # additional RGB sensor I configured

        print("created habitat_plant succsefully")

    def _render(self):
        # self.env._update_step_stats()  # think this increments episode count
        sim_obs = self.env._sim.get_sensor_observations()
        self.observations = self.env._sim._sensor_suite.get_observations(sim_obs)
        self.observations.update(
            self.env._task.sensor_suite.get_observations(
                observations=self.observations, episode=self.env.current_episode
            )
        )
    

    def _update_position(self):
        state = self.env.sim.get_agent_state(0)
        heading_vector = quaternion_rotate_vector(
            state.rotation.inverse(), np.array([0, 0, -1])
        )
        phi = cartesian_to_polar(-heading_vector[2], heading_vector[0])[1]
        top_down_map_angle = phi - np.pi / 2 
        agent_pos = state.position
        agent_quat = quat_to_coeff(state.rotation)
        euler = list(tf.transformations.euler_from_quaternion(agent_quat))
        proj_quat = tf.transformations.quaternion_from_euler(0.0,0.0,top_down_map_angle-np.pi)
        # proj_quat = tf.transformations.quaternion_from_euler(euler[0]+np.pi,euler[2],euler[1])
        agent_pos_in_map_frame = convert_points_to_topdown(self.env.sim.pathfinder, [agent_pos])
        self.poseMsg = PoseStamped()
        self.poseMsg.header.frame_id = "world"
        self.poseMsg.pose.orientation.x = proj_quat[0]
        self.poseMsg.pose.orientation.y = proj_quat[2]
        self.poseMsg.pose.orientation.z = proj_quat[1]
        self.poseMsg.pose.orientation.w = proj_quat[3]
        self.poseMsg.header.stamp = rospy.Time.now()
        self.poseMsg.pose.position.x = agent_pos_in_map_frame[0][0]
        self.poseMsg.pose.position.y = agent_pos_in_map_frame[0][1]
        self.poseMsg.pose.position.z = 0.0
        self._pub_pose.publish(self.poseMsg)
        vz = -state.velocity[0]
        vx = state.velocity[1]
        dt = self._dt

        start_pos = self.env._sim.agents[0].scene_node.absolute_translation

        ax = (
            self.env._sim.agents[0]
            .scene_node.absolute_transformation()[self._z_axis]
            .xyz
        )
        self.env._sim.agents[0].scene_node.translate_local(ax * vz * dt)

        ax = (
            self.env._sim.agents[0]
            .scene_node.absolute_transformation()[self._x_axis]
            .xyz
        )
        self.env._sim.agents[0].scene_node.translate_local(ax * vx * dt)

        end_pos = self.env._sim.agents[0].scene_node.absolute_translation
        filter_end = self.env._sim.agents[0].move_filter_fn(start_pos, end_pos)
        # Update the position to respect the filter
        self.env._sim.agents[0].scene_node.translate(filter_end - end_pos)
        # self._render()

    def _update_attitude(self):
        """ update agent orientation given angular velocity and delta time"""
        state = self.env.sim.get_agent_state(0)
        yaw = state.angular_velocity[2] / 3.1415926 * 180
        dt = self._dt

        _rotate_local_fns = [
            hsim.SceneNode.rotate_x_local,
            hsim.SceneNode.rotate_y_local,
            hsim.SceneNode.rotate_z_local,
        ]
        _rotate_local_fns[self._y_axis](
            self.env._sim.agents[0].scene_node, mn.Deg(yaw * dt)
        )
        self.env._sim.agents[0].scene_node.rotation = self.env._sim.agents[
            0
        ].scene_node.rotation.normalized()
        # self._render()

    def run(self):
        """Publish sensor readings through ROS on a different thread.
            This method defines what the thread does when the start() method
            of the threading class is called
        """
        while not rospy.is_shutdown():
            lock.acquire()
            rgb_with_res = np.concatenate(
                (
                    np.float32(self.observations["rgb"].ravel()),
                    np.array(
                        [self._sensor_resolution["RGB"], self._sensor_resolution["RGB"]]
                    ),
                )
            )

            # multiply by 10 to get distance in meters
            depth_with_res = np.concatenate(
                (
                    np.float32(self.observations["depth"].ravel() * 10),
                    np.array(
                        [
                            self._sensor_resolution["DEPTH"],
                            self._sensor_resolution["DEPTH"],
                        ]
                    ),
                )
            )

            lock.release()
            self._pub_rgb.publish(np.float32(rgb_with_res))
            self._pub_depth.publish(np.float32(depth_with_res))

            self._r.sleep()
            

    def set_linear_velocity(self, vx, vy):
        self.env._sim.agents[0].state.velocity[0] = vx
        self.env._sim.agents[0].state.velocity[1] = vy

    def set_yaw(self, yaw):
        self.env._sim.agents[0].state.angular_velocity[2] = yaw

    def update_orientation(self):
        lock.acquire()
        self._update_attitude()
        self._update_position()
        self._render()
        lock.release()

    def set_dt(self, dt):
        self._dt = dt



def callback(vel, my_env):
    lock.acquire()
    my_env.set_linear_velocity(vel.linear.x, vel.linear.y)
    my_env.set_yaw(vel.angular.z)
    lock.release()


def main():

    my_env = sim_env(env_config_file="configs/tasks/pointnav_rgbd.yaml")
    # start the thread that publishes sensor readings
    my_env.start()

    rospy.Subscriber("/cmd_vel", Twist, callback, (my_env), queue_size=1)
    # define a list capturing how long it took
    # to update agent orientation for past 3 instances
    # TODO modify dt_list to depend on r1
    dt_list = [0.009, 0.009, 0.009]
    while not rospy.is_shutdown():

        start_time = time.time()
        # cv2.imshow("bc_sensor", my_env.observations['bc_sensor'])
        # cv2.waitKey(100)
        # time.sleep(0.1)
        my_env.update_orientation()

        dt_list.insert(0, time.time() - start_time)
        dt_list.pop()
        my_env.set_dt(sum(dt_list) / len(dt_list))


if __name__ == "__main__":
    main()