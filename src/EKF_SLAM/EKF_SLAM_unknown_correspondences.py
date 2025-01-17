#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Implementation of EKF SLAM with unknown correspondences.
See Probabilistic Robotics:
    1. Page 321, Table 10.2 for full algorithm.

'''

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


class ExtendedKalmanFilterSLAM():
    def __init__(self, dataset, robot, start_frame, end_frame, R, Q, plot, plot_inter):
        self.load_data(dataset, robot, start_frame, end_frame)
        self.initialization(R, Q)
        for data in self.data:
            if (data[1] == -1):
                self.motion_update(data)
            else:
                self.data_association(data)
                self.measurement_update(data)
            # Plot every n frames
            if plot and plot_inter:
                if (len(self.states) > (800 - start_frame) and len(self.states) % 30 == 0):
                    self.plot_data()
        if plot: self.plot_data()

    def load_data(self, dataset, robot, start_frame, end_frame):
        # Loading dataset
        # Barcodes: [Subject#, Barcode#]
        self.barcodes_data = np.loadtxt(dataset + "/Barcodes.dat")
        # Ground truth: [Time[s], x[m], y[m], orientation[rad]]
        self.groundtruth_data = np.loadtxt(dataset + "/" + robot +"_Groundtruth.dat")
        #self.groundtruth_data = self.groundtruth_data[2000:] # Remove initial readings
        # Landmark ground truth: [Subject#, x[m], y[m]]
        self.landmark_groundtruth_data = np.loadtxt(dataset + "/Landmark_Groundtruth.dat")
        # Measurement: [Time[s], Subject#, range[m], bearing[rad]]
        self.measurement_data = np.loadtxt(dataset + "/" + robot +"_Measurement.dat")
        # Odometry: [Time[s], Subject#, forward_V[m/s], angular _v[rad/s]]
        self.odometry_data = np.loadtxt(dataset + "/" + robot +"_Odometry.dat")

        # Collect all input data and sort by timestamp
        # Add subject "odom" = -1 for odometry data
        odom_data = np.insert(self.odometry_data, 1, -1, axis=1)
        self.data = np.concatenate((odom_data, self.measurement_data), axis=0)
        self.data = self.data[np.argsort(self.data[:, 0])]

        # Select data according to start_frame and end_frame
        # Fisrt frame must be control input
        while self.data[start_frame][1] != -1:
            start_frame += 1
        # Remove all data before start_frame and after the end_timestamp
        self.data = self.data[start_frame:end_frame]
        start_timestamp = self.data[0][0]
        end_timestamp = self.data[-1][0]
        # Remove all groundtruth outside the range
        for i in range(len(self.groundtruth_data)):
            if (self.groundtruth_data[i][0] >= end_timestamp):
                break
        self.groundtruth_data = self.groundtruth_data[:i]
        for i in range(len(self.groundtruth_data)):
            if (self.groundtruth_data[i][0] >= start_timestamp):
                break
        self.groundtruth_data = self.groundtruth_data[i:]

        # Combine barcode Subject# with landmark Subject#
        # Lookup table to map barcode Subjec# to landmark coordinates
        # [x[m], y[m], x std-dev[m], y std-dev[m]]
        # Ground truth data is not used in EKF SLAM
        self.landmark_locations = {}
        for i in range(5, len(self.barcodes_data), 1):
            self.landmark_locations[self.barcodes_data[i][1]] = self.landmark_groundtruth_data[i - 5][1:]

        # Lookup table to map barcode Subjec# to landmark Subject#
        # Barcode 6 is the first landmark (1 - 15 for 6 - 20)
        # Landmark association is not used for unknown Correspondences
        self.landmark_indexes = {}
        for i in range(5, len(self.barcodes_data), 1):
            self.landmark_indexes[self.barcodes_data[i][1]] = i - 4

        # Table to record if each landmark has been seen or not
        # Element [0] is not used. [1] - [15] represent for landmark# 6 - 20
        self.landmark_observed = np.full(len(self.landmark_indexes) + 1, False)

    def initialization(self, R, Q):
        # Initial state: 3 for robot, 2 for each landmark
        # To simplify, use fixed number of landmarks for states and covariances
        self.states = np.zeros((1, 3 + 2 * len(self.landmark_indexes)))
        self.states[0][:3] = self.groundtruth_data[0][1:]
        self.last_timestamp = self.groundtruth_data[0][0]
        self.stamps = []
        self.stamps.append(self.last_timestamp)

        # EKF state covariance: (3 + 2n) x (3 + 2n)
        # For robot states, use first ground truth data as initial value
        #   - small values for top-left 3 x 3 matrix
        # For landmark states, we have no information at the beginning
        #   - large values for rest of variances (diagonal) data
        #   - small values for all covariances (off-diagonal) data
        self.sigma = 1e-6 * np.full((3 + 2 * len(self.landmark_indexes), 3 + 2 * len(self.landmark_indexes)), 1)
        for i in range(3, 3 + 2 * len(self.landmark_indexes)):
            self.sigma[i][i] = 1e6

        # State covariance matrix
        self.R = R
        # Measurement covariance matrix
        self.Q = Q

    def motion_update(self, control):
        # ------------------ Step 1: Mean update ---------------------#
        # State: [x, y, θ, x_l1, y_l1, ......, x_ln, y_ln]
        # Control: [v, w]
        # Only robot state is updated during each motion update step!
        # [x_t, y_t, θ_t] = g(u_t, x_t-1)
        #   x_t  =  x_t-1 + v * cosθ_t-1 * delta_t
        #   y_t  =  y_t-1 + v * sinθ_t-1 * delta_t
        #   θ_t  =  θ_t-1 + w * delta_t
        # Skip motion update if two odometry data are too close
        delta_t = control[0] - self.last_timestamp
        if (delta_t < 0.001):
            return
        # Compute updated [x, y, theta]
        x_t = self.states[-1][0] + control[2] * np.cos(self.states[-1][2]) * delta_t
        y_t = self.states[-1][1] + control[2] * np.sin(self.states[-1][2]) * delta_t
        theta_t = self.states[-1][2] + control[3] * delta_t
        # Limit θ within [-pi, pi]
        if (theta_t > np.pi):
            theta_t -= 2 * np.pi
        elif (theta_t < -np.pi):
            theta_t += 2 * np.pi
        self.last_timestamp = control[0]
        # Append new state
        new_state = np.copy(self.states[-1])
        new_state[0] = x_t
        new_state[1] = y_t
        new_state[2] = theta_t
        self.states = np.append(self.states, np.array([new_state]), axis=0)
        self.stamps.append(self.last_timestamp)

        # ------ Step 2: Linearize state-transition by Jacobian ------#
        # Jacobian of motion: G = d g(u_t, x_t-1) / d x_t-1
        #         1  0  -v * delta_t * sinθ_t-1
        #   G  =  0  1   v * delta_t * cosθ_t-1        0
        #         0  0             1
        #
        #                      0                    I(2n x 2n)
        self.G = np.identity(3 + 2 * len(self.landmark_indexes))
        self.G[0][2] = - control[2] * delta_t * np.sin(self.states[-2][2])
        self.G[1][2] = control[2] * delta_t * np.cos(self.states[-2][2])

        # ---------------- Step 3: Covariance update ------------------#
        # sigma = G x sigma x G.T + Fx.T x R x Fx
        self.sigma = self.G.dot(self.sigma).dot(self.G.T)
        self.sigma[0][0] += self.R[0][0]
        self.sigma[1][1] += self.R[1][1]
        self.sigma[2][2] += self.R[2][2]

    def data_association(self, measurement):
        # Return if this measurement is not from a a landmark (other robots)
        if not measurement[1] in self.landmark_indexes:
            return

        # Get current robot state, measurement
        x_t = self.states[-1][0]
        y_t = self.states[-1][1]
        theta_t = self.states[-1][2]
        range_t = measurement[2]
        bearing_t = measurement[3]

        # The expected landmark's location based on current robot state and measurement
        #   x_l = x_t + range_t * cos(bearing_t + theta_t)
        #   y_l = y_t + range_t * sin(bearing_t + theta_t)
        landmark_x_expected = x_t + range_t * np.cos(bearing_t + theta_t)
        landmark_y_expected = y_t + range_t * np.sin(bearing_t + theta_t)

        # If the current landmark has not been seen, initilize its location as the expected one
        landmark_idx = self.landmark_indexes[measurement[1]]
        self.landmark_idx = landmark_idx
        if not self.landmark_observed[landmark_idx]:
            self.landmark_observed[landmark_idx] = True
            self.states[-1][2 * landmark_idx + 1] = landmark_x_expected
            self.states[-1][2 * landmark_idx + 2] = landmark_y_expected

        # Calculate the Likelihood for each existed landmark
        min_distance = 1e16
        for i in range(1, len(self.landmark_indexes) + 1):
            # Continue if this landmark has not been observed
            if not self.landmark_observed[i]:
                continue

            # Get current landmark estimate
            x_l = self.states[-1][2 * i + 1]
            y_l = self.states[-1][2 * i + 2]

            # Calculate expected range and bearing measurement
            #   range   =  sqrt((x_l - x_t)^2 + (y_l - y_t)^2)
            #  bearing  =  atan2((y_l - y_t) / (x_l - x_t)) - θ_t
            delta_x = x_l - x_t
            delta_y = y_l - y_t
            q = delta_x ** 2 + delta_y ** 2
            range_expected = np.sqrt(q)
            bearing_expected = np.arctan2(delta_y, delta_x) - theta_t

            # Compute Jacobian H of Measurement Model
            # Landmark state becomes a variable in measurement model
            # Jacobian: H = d h(x_t, x_l) / d (x_t, x_l)
            #        1 0 0  0 ...... 0   0 0   0 ...... 0
            #        0 1 0  0 ...... 0   0 0   0 ...... 0
            # F_x =  0 0 1  0 ...... 0   0 0   0 ...... 0
            #        0 0 0  0 ...... 0   1 0   0 ...... 0
            #        0 0 0  0 ...... 0   0 1   0 ...... 0
            #          (2*landmark_idx - 2)
            #          -delta_x/√q  -delta_y/√q  0  delta_x/√q  delta_y/√q
            # H_low =   delta_y/q   -delta_x/q  -1  -delta_y/q  delta_x/q
            #               0            0       0       0          0
            # H = H_low x F_x
            F_x = np.zeros((5, 3 + 2 * len(self.landmark_indexes)))
            F_x[0][0] = 1.0
            F_x[1][1] = 1.0
            F_x[2][2] = 1.0
            F_x[3][2 * i + 1] = 1.0
            F_x[4][2 * i + 2] = 1.0
            H_1 = np.array([-delta_x/np.sqrt(q), -delta_y/np.sqrt(q), 0, delta_x/np.sqrt(q), delta_y/np.sqrt(q)])
            H_2 = np.array([delta_y/q, -delta_x/q, -1, -delta_y/q, delta_x/q])
            H_3 = np.array([0, 0, 0, 0, 0])
            H = np.array([H_1, H_2, H_3]).dot(F_x)

            # Compute Mahalanobis distance
            Psi = H.dot(self.sigma).dot(H.T) + self.Q
            difference = np.array([range_t - range_expected, bearing_t - bearing_expected, 0])
            Pi = difference.T.dot(np.linalg.inv(Psi)).dot(difference)

            # Get landmark information with least distance
            if Pi < min_distance:
                min_distance = Pi
                # Values for measurement update
                self.H = H
                self.Psi = Psi
                self.difference = difference
                # Values for plotting data association
                self.landmark_expected = np.array([landmark_x_expected, landmark_y_expected])
                self.landmark_current = np.array([x_l, y_l])

    def measurement_update(self, measurement):
        # Return if this measurement is not from a a landmark (other robots)
        if not measurement[1] in self.landmark_indexes:
            return

        # Update mean
        self.K = self.sigma.dot(self.H.T).dot(np.linalg.inv(self.Psi))
        innovation = self.K.dot(self.difference)
        new_state = self.states[-1] + innovation
        self.states = np.append(self.states, np.array([new_state]), axis=0)
        self.last_timestamp = measurement[0]
        self.stamps.append(self.last_timestamp)

        # Update covariance
        self.sigma = (np.identity(3 + 2 * len(self.landmark_indexes)) - self.K.dot(self.H)).dot(self.sigma)

    def plot_data(self):
        # Clear all
        plt.cla()

        # Ground truth data
        plt.plot(self.groundtruth_data[:, 1], self.groundtruth_data[:, 2], 'b', label='Robot State Ground truth')

        # States
        plt.plot(self.states[:, 0], self.states[:, 1], 'r', label='Robot State Estimate')

        # Start and end points
        plt.plot(self.groundtruth_data[0, 1], self.groundtruth_data[0, 2], 'g8', markersize=12, label='Start point')
        plt.plot(self.groundtruth_data[-1, 1], self.groundtruth_data[-1, 2], 'y8', markersize=12, label='End point')

        # Landmark ground truth locations and indexes
        landmark_xs = []
        landmark_ys = []
        for location in self.landmark_locations:
            landmark_xs.append(self.landmark_locations[location][0])
            landmark_ys.append(self.landmark_locations[location][1])
            index = self.landmark_indexes[location] + 5
            plt.text(landmark_xs[-1], landmark_ys[-1], str(index), alpha=0.5, fontsize=10)
        plt.scatter(landmark_xs, landmark_ys, s=200, c='k', alpha=0.2, marker='*', label='Landmark Ground Truth')

        # Landmark estimated locations
        estimate_xs = []
        estimate_ys = []
        for i in range(1, len(self.landmark_indexes) + 1):
            if self.landmark_observed[i]:
                estimate_xs.append(self.states[-1][2 * i + 1])
                estimate_ys.append(self.states[-1][2 * i + 2])
                plt.text(estimate_xs[-1], estimate_ys[-1], str(i+5), fontsize=10)
        plt.scatter(estimate_xs, estimate_ys, s=50, c='k', marker='.', label='Landmark Estimate')

        # Line pointing from current observed landmark to associated landmark
        xs = [self.landmark_expected[0], self.landmark_current[0]]
        ys = [self.landmark_expected[1], self.landmark_current[1]]
        plt.plot(xs, ys, color='c', label='Data Association')
        plt.text(self.landmark_expected[0], self.landmark_expected[1], str(self.landmark_idx+5))

        # Expected location of current observed landmark
        plt.scatter(self.landmark_expected[0], self.landmark_expected[1], s=100, c='r', alpha=0.5, marker='P', label='Current Observed Landmark')

        plt.title('EKF SLAM with Unknown Correspondences')
        plt.legend(loc='upper right')
        plt.xlim((-2.0, 6.5))
        plt.ylim((-7.0, 7.0))
        plt.pause(0.5)

    def build_dataframes(self):
        self.gt = build_timeseries(self.groundtruth_data, cols=['stamp','x','y','theta'])
        data = np.array(self.states[:,:3])
        stamp = np.expand_dims(self.stamps, axis=1)
        data_s = np.hstack([stamp,data])
        self.robot_states = build_timeseries(data_s, cols=['stamp','x','y','theta'])
        
def build_timeseries(data,cols):
    timeseries = pd.DataFrame(data, columns=cols)
    timeseries['stamp'] = pd.to_datetime(timeseries['stamp'], unit='s')
    timeseries = timeseries.set_index('stamp')
    return timeseries

def build_state_timeseries(stamp,data,cols):
    timeseries = pd.DataFrame(data, columns=cols)
    timeseries['stamp'] = pd.to_datetime(stamp, unit='s')
    timeseries = timeseries.set_index('stamp')
    return timeseries

def filter_static_landmarks(lm, barcodes):
    for L,l in dict(barcodes).items(): # Translate barcode num to landmark num
        lm[lm==l]=L
    lm = lm[lm.type > 5] # Keep only static landmarks 
    return lm 

if __name__ == "__main__":
    # Dataset 1
    dataset = "data/MRCLAM_Dataset1"
    robot = 'Robot1' # Robot
    start_frame = 800
    end_frame = 3200
    # State covariance matrix
    R = np.diagflat(np.array([120.0, 120.0, 100.0])) ** 2
    # Measurement covariance matrix
    Q = np.diagflat(np.array([1000.0, 1000.0, 1e16])) ** 2

    ekf_slam = ExtendedKalmanFilterSLAM(dataset, robot, start_frame, end_frame, R, Q, True, True)

    plt.show()
