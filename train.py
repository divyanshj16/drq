import copy
import math
import os
import pickle as pkl
import sys
import time

import numpy as np

import dmc2gym
import hydra
import torch
import torch.nn as nn
import torch.nn.functional as F
import utils
from logger import Logger
from replay_buffer import ReplayBuffer
from video import VideoRecorder
import robosuite as suite
from robosuite.renderers.igibson.igibson_wrapper import iGibsonWrapper
from robosuite.wrappers import GymWrapper
import robosuite.utils.macros as macros

macros.IMAGE_CONVENTION = "opencv"

torch.backends.cudnn.benchmark = True


def make_env(cfg):
    """Helper function to create dm_control environment"""
    if cfg.env == 'ball_in_cup_catch':
        domain_name = 'ball_in_cup'
        task_name = 'catch'
    elif cfg.env == 'point_mass_easy':
        domain_name = 'point_mass'
        task_name = 'easy'
    else:
        domain_name = cfg.env.split('_')[0]
        task_name = '_'.join(cfg.env.split('_')[1:])

    # per dreamer: https://github.com/danijar/dreamer/blob/02f0210f5991c7710826ca7881f19c64a012290c/wrappers.py#L26
    camera_id = 2 if domain_name == 'quadruped' else 0

    # Deafult
    # env = dmc2gym.make(domain_name=domain_name,
    #                    task_name=task_name,
    #                    seed=cfg.seed,
    #                    visualize_reward=False,
    #                    from_pixels=True,
    #                    height=cfg.image_size,
    #                    width=cfg.image_size,
    #                    frame_skip=cfg.action_repeat,
    #                    camera_id=camera_id)

    # iGibson
    # env = GymWrapper(iGibsonWrapper(
    #                     suite.make(
    #                             "Lift",
    #                             robots = ["IIWA"],
    #                             reward_shaping=True,
    #                             has_renderer=False,           
    #                             has_offscreen_renderer=True,
    #                             ignore_done=False,
    #                             use_object_obs=False,
    #                             use_camera_obs=True,  
    #                             render_camera='frontview',
    #                             control_freq=20, 
    #                             camera_names=['agentview'],
    #                             render_with_igibson=True
    #                         ),
    #                     enable_pbr=True,
    #                     enable_shadow=True,
    #                     modes=('rgb',), #, 'seg', '3d', 'normal'),
    #                     render2tensor=False,
    #                     optimized=False,
    #                     width=cfg.image_size,
    #                     height=cfg.image_size
    #                 ),
    # keys=['agentview_image']
    # )

    # Vanilla robosuite.
    env = GymWrapper(
        suite.make(
                "Lift",
                robots = ["IIWA"],
                reward_shaping=True,
                has_renderer=False,           
                has_offscreen_renderer=True,
                ignore_done=False,
                use_object_obs=True,
                use_camera_obs=True,  
                render_camera='frontview',
                control_freq=20, 
                camera_names=['agentview'],
                camera_heights=cfg.image_size,
                camera_widths=cfg.image_size,
                render_with_igibson=False
            ),
            keys=['agentview_image']
    )    
    

    env._max_episode_steps = 250

    env = utils.FrameStack(env, k=cfg.frame_stack)

    env.seed(cfg.seed)
    assert env.action_space.low.min() >= -1
    assert env.action_space.high.max() <= 1

    return env


class Workspace(object):
    def __init__(self, cfg):
        self.work_dir = os.getcwd()
        print(f'workspace: {self.work_dir}')

        self.cfg = cfg

        self.logger = Logger(self.work_dir,
                             save_tb=cfg.log_save_tb,
                             log_frequency=cfg.log_frequency_step,
                             agent=cfg.agent.name,
                             action_repeat=cfg.action_repeat)

        utils.set_seed_everywhere(cfg.seed)
        self.device = torch.device(cfg.device)
        self.env = make_env(cfg)

        cfg.agent.params.obs_shape = self.env.observation_space.shape
        cfg.agent.params.action_shape = self.env.action_space.shape
        cfg.agent.params.action_range = [
            float(self.env.action_space.low.min()),
            float(self.env.action_space.high.max())
        ]
        self.agent = hydra.utils.instantiate(cfg.agent)

        self.replay_buffer = ReplayBuffer(self.env.observation_space.shape,
                                          self.env.action_space.shape,
                                          cfg.replay_buffer_capacity,
                                          self.cfg.image_pad, self.device)

        self.video_recorder = VideoRecorder(
            self.work_dir if cfg.save_video else None)
        self.step = 0

    def evaluate(self):
        # import pdb; pdb.set_trace();
        average_episode_reward = 0
        for episode in range(self.cfg.num_eval_episodes):
            obs = self.env.reset()
            self.video_recorder.init(enabled=(episode == 0))
            done = False
            episode_reward = 0
            episode_step = 0
            while not done:
                with utils.eval_mode(self.agent):
                    action = self.agent.act(obs, sample=False)
                obs, reward, done, info = self.env.step(action)
                self.video_recorder.record(obs)
                episode_reward += reward
                episode_step += 1

            average_episode_reward += episode_reward
            self.video_recorder.save(f'{self.step}.mp4')
        average_episode_reward /= self.cfg.num_eval_episodes
        self.logger.log('eval/episode_reward', average_episode_reward,
                        self.step)
        self.logger.dump(self.step)

    def run(self):
        episode, episode_reward, episode_step, done = 0, 0, 1, True
        start_time = time.time()
        while self.step < self.cfg.num_train_steps:
            if done:
                if self.step > 0:
                    self.logger.log('train/duration',
                                    time.time() - start_time, self.step)
                    start_time = time.time()
                    self.logger.dump(
                        self.step, save=(self.step > self.cfg.num_seed_steps))

                # evaluate agent periodically
                if self.step % self.cfg.eval_frequency == 0:
                    self.logger.log('eval/episode', episode, self.step)
                    self.evaluate()

                self.logger.log('train/episode_reward', episode_reward,
                                self.step)

                obs = self.env.reset()
                done = False
                episode_reward = 0
                episode_step = 0
                episode += 1

                self.logger.log('train/episode', episode, self.step)

            # sample action for data collection
            if self.step < self.cfg.num_seed_steps:
                action = self.env.action_space.sample()
            else:
                with utils.eval_mode(self.agent):
                    action = self.agent.act(obs, sample=True)

            # run training update
            if self.step >= self.cfg.num_seed_steps:
                for _ in range(self.cfg.num_train_iters):
                    self.agent.update(self.replay_buffer, self.logger,
                                      self.step)

            next_obs, reward, done, info = self.env.step(action)
    
            # allow infinite bootstrap
            done = float(done)
            done_no_max = 0 if episode_step + 1 == self.env._max_episode_steps else done
            episode_reward += reward

            self.replay_buffer.add(obs, action, reward, next_obs, done,
                                   done_no_max)

            obs = next_obs
            episode_step += 1
            self.step += 1


@hydra.main(config_path='config.yaml', strict=True)
def main(cfg):
    from train import Workspace as W
    workspace = W(cfg)
    workspace.run()


if __name__ == '__main__':
    main()
