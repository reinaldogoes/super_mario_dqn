import os
os.environ["TF_FORCE_GPU_ALLOW_GROWTH"] = "true"

import acme
import tensorflow as tf
import sonnet as snt
from acme.tf.networks import duelling
from acme.utils import loggers
import matplotlib.pyplot as plt
import numpy as np

from mario_env import MarioEnvironment
from utils.env_loop import EnvironmentLoop
from utils.utils import restore_module
import time
from datetime import datetime
import pickle

import expert_demonstration
from dqn.agent import DQN as DQNAgent
from pathlib import Path
import re
import ray
import matplotlib.pyplot as plt


def make_dqn(num_actions: int):
    return snt.Sequential([
        snt.Conv2D(32, [3, 3], [2, 2]),
        tf.nn.relu,
        snt.Conv2D(32, [3, 3], [2, 2]),
        tf.nn.relu,
        snt.Conv2D(32, [3, 3], [2, 2]),
        tf.nn.relu,
        snt.Conv2D(32, [3, 3], [2, 2]),
        tf.nn.relu,
        snt.Flatten(),
        duelling.DuellingMLP(num_actions, hidden_sizes=[512]),
    ])


def make_env():
    return MarioEnvironment(skip_frames=3,
                            img_rescale_pc=0.5,
                            stack_func=np.dstack,
                            stack_mode="all",
                            grayscale=True,
                            black_background=True,
                            in_game_score_weight=0.02,
                            movement_type="right_only",
                            world_and_level=(3, 1),
                            idle_frames_threshold=1000)


def train(network=None, expert_data_path=None):
    env = make_env()
    env_spec = acme.make_environment_spec(env)

    if network is None:
        network = make_dqn(env_spec.actions.num_values)

    expert_data = None
    if expert_data_path is not None:
        with open(expert_data_path, "rb") as handle:
            expert_data = pickle.load(handle)
        num_timesteps = np.sum([1 + len(ep["mid"]) for ep in expert_data])
        print(f"Using expert data from {expert_data_path}. "
              f"Episodes: {len(expert_data)}. Timesteps: {num_timesteps}.")

    agent = DQNAgent(environment_spec=env_spec,
                     network=network,
                     batch_size=32,
                     learning_rate=6.25e-5,
                     logger=loggers.NoOpLogger(),
                     min_replay_size=2500,
                     max_replay_size=int(2e5),
                     target_update_period=2500,
                     epsilon=tf.Variable(0.015),
                     n_step=10,
                     discount=0.9,
                     expert_data=expert_data)

    loop = EnvironmentLoop(environment=env,
                           actor=agent,
                           module2save=network)
    reward_history = loop.run(num_steps=int(1e5),
                              render=True,
                              checkpoint=True,
                              checkpoint_freq=10)

    avg_hist = [np.mean(reward_history[i:(i+50)])
                for i in range(len(reward_history) - 50)]
    plt.plot(list(range(len(avg_hist))), avg_hist)
    plt.show()

    env.close()
    return network


def eval_policy(policy, num_episodes, fps=0, epsilon_greedy=0.025):
    # TODO: plot Q values
    # TODO: Jacobian matrix (sensitiviy to the different regions of the input)
    env = make_env()
    env.reset()
    env.render()
    input("\nPress [ENTER] to continue.")

    for episode in range(num_episodes):
        obs = env.reset().observation
        episode_reward = 0.0

        done = False
        while not done:
            # Rendering:
            env.render()
            time.sleep(1 / fps)

            # Q-values:
            obs = tf.Variable(tf.expand_dims(obs, axis=0))
            with tf.GradientTape() as tape:
                tape.watch(obs)
                q_values = policy(obs)[0]
                max_q = q_values[tf.argmax(q_values)]

            # gradients = tape.gradient(max_q, obs)[0, :, :, -1]
            # gradients = tf.abs(gradients)
            # gradients = tf.math.divide(tf.subtract(gradients,
            #                                        tf.reduce_min(gradients)),
            #                            tf.subtract(tf.reduce_max(gradients),
            #                                        tf.reduce_min(gradients)))
            #
            # input_img = obs[0, :, :, -1]
            # assert gradients.shape == input_img.shape
            # relevance_img = tf.stack([gradients, input_img, input_img], axis=-1)
            #
            # plt.imshow(relevance_img)
            # plt.show()

            # Random action:
            if np.random.uniform(low=0, high=1) < epsilon_greedy:
                action = np.random.randint(low=0,
                                           high=env.action_spec().num_values)
            # Greedy policy:
            else:
                action = tf.argmax(q_values)

            # Environment step:
            timestep_obj = env.step(action)
            obs = timestep_obj.observation

            episode_reward += timestep_obj.reward
            done = timestep_obj.last()

        print(f"Episode reward: {episode_reward}")


def collect_data_from_human():
    data = expert_demonstration.human_play(make_env(), num_episodes=20)

    date_and_time = datetime.today().strftime('%Y-%m-%d-%H-%M-%S')
    save_path = f"./human_data/data_{date_and_time}.pkl"
    with open(save_path, "wb") as handle:
        pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)

    count = 0
    for ep_data in data:
        count = 1 + len(ep_data["mid"])
    print(f"\nCollected data from {count} timesteps.\n")


def find_best_policy(folder_path):
    ray.init(ignore_reinit_error=True)

    @ray.remote
    def _eval_policy_ray(policy_path):
        env = make_env()
        policy = make_dqn(env.action_spec().num_values)
        restore_module(base_module=policy, save_path=policy_path)

        obs = env.reset().observation
        policy_reward = 0
        done = False
        while not done:
            q_values = policy(tf.expand_dims(obs, axis=0))[0]
            action = tf.argmax(q_values)

            timestep_obj = env.step(action)
            obs = timestep_obj.observation

            policy_reward += timestep_obj.reward
            done = timestep_obj.last()

        return policy_reward

    # Getting files names:
    files = []
    for i, fn in enumerate(sorted(Path(folder_path).iterdir(),
                                  key=os.path.getmtime)):
        if i % 2 == 0:
            files.append(re.search("^[^.]*", str(fn))[0])

    # Searching:
    futures = [_eval_policy_ray.remote(fn) for fn in files]
    rewards = ray.get(futures)

    best_policy_path = files[np.argmax(rewards)]
    best_policy_reward = np.max(rewards)

    print(f"Best policy found at: {best_policy_path}")
    print(f"Best policy total reward: {best_policy_reward}")

    return best_policy_path


if __name__ == "__main__":
    # collect_data_from_human()
    # policy_path = find_best_policy("checkpoints/checkpoints_2021-03-29-20-36-19")
    policy_path = "checkpoints/best_policies/w3_lv1/w3_lv1_completed_r3175"

    policy_network = make_dqn(make_env().action_spec().num_values)
    restore_module(base_module=policy_network, save_path=policy_path)
    print(f"\nUsing policy checkpoint from: {policy_path}")

    # train(policy_network, expert_data_path=None)
    eval_policy(policy_network, num_episodes=3, fps=60, epsilon_greedy=0)

    # env = make_env()
    # obs = env.reset().observation
    # while True:
    #     env.render()
    #     env.plot_obs(np.hstack([obs[:, :, i] for i in range(obs.shape[-1])]))
    #     obs = env.step(
    #         np.random.randint(low=0, high=env.action_spec().num_values)
    #     ).observation

    # env = make_env()
    # print(env.reset().observation.shape)
    # network = make_dqn(env.action_spec().num_values)
    # out = network(tf.expand_dims(env.reset().observation, axis=0))
    # print(out)
    # print(out.shape)
