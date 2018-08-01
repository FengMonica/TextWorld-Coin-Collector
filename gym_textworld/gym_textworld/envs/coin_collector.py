import os
import sys
import json
import numpy as np

from six import StringIO

import gym
from gym.utils import colorize

import textworld
from textworld import g_rng
from textworld.utils import uniquify
try:
    from textworld.logic import Variable, Proposition
except ModuleNotFoundError:
    print("*** Deprecated: textworld.generator.logic has been moved to textworld.logic. Please update TextWorld. ***")
    from textworld.generator.logic import Variable, Proposition

from textworld.generator import Quest, World
from textworld.generator.logger import GameLogger
from textworld.generator import data
from textworld.generator.vtypes import get_new
from textworld.generator.graph_networks import reverse_direction

from gym_textworld import spaces as text_spaces
from gym_textworld.utils import make_infinite_shuffled_iterator

from hashids import Hashids


def encode_seeds(seeds):
    hashids = Hashids(salt="TextWorld")
    return hashids.encode(*seeds)


def make_coin_collector_game_from_level(level, grammar_flags, seeds):
    """
    Level difficulties are defined as follow:
      Level   1 to 100: Nb. rooms = level, quest length = level
      Level 101 to 200: Nb. rooms = 2 * (level % 100), quest length = level % 100,
        distractors rooms added along the chain.
      Level 201 to 300: Nb. rooms = 3 * (level % 100), quest length = level % 100,
        distractors rooms *randomly* added along the chain.
      ...
    """
    n_distractors = (level // 100)
    quest_length = level % 100
    n_rooms = (n_distractors + 1) * quest_length
    distractor_mode = "random" if n_distractors > 2 else "simple"
    return make_coin_collector_game(n_rooms, quest_length, distractor_mode, grammar_flags, seeds)


def make_coin_collector_game(n_rooms, quest_length, distractor_mode, grammar_flags, seeds):
    if distractor_mode == "simple" and float(n_rooms) / quest_length > 4:
        msg = "Total number of rooms must be less than 4 * `quest_length` when distractor mode is 'simple'."
        raise ValueError(msg)

    metadata = {}  # Collect infos for reproducibility.
    metadata["seeds"] = seeds
    metadata["world_size"] = n_rooms
    metadata["quest_length"] = quest_length
    metadata["grammar_flags"] = grammar_flags

    rng_map = np.random.RandomState(seeds['seed_map'])
    # rng_objects = np.random.RandomState(seeds['seed_objects'])
    # rng_quest = np.random.RandomState(seeds['seed_quest'])
    rng_grammar = np.random.RandomState(seeds['seed_grammar'])

    # Generate map.
    M = textworld.GameMaker()
    M.grammar = textworld.generator.make_grammar(flags=grammar_flags, rng=rng_grammar)

    rooms = []
    walkthrough = []
    for i in range(quest_length):
        r = M.new_room()
        if i >= 1:
            # Connect it to the previous rooms.
            free_exits = [k for k, v in rooms[-1].exits.items() if v.dest is None]
            src_exit = rng_map.choice(free_exits)
            dest_exit = reverse_direction(src_exit)
            M.connect(rooms[-1].exits[src_exit], r.exits[dest_exit])
            walkthrough.append("go {}".format(src_exit))

        rooms.append(r)

    M.set_player(rooms[0])

    # Add object the player has to pick up.
    obj = M.new(type="o", name="coin")
    rooms[-1].add(obj)

    # Add distractor rooms, if needed.
    chain_of_rooms = list(rooms)
    while len(rooms) < n_rooms:
        if distractor_mode == "random":
            src = rng_map.choice(rooms)
        else:
            # Add one distractor room per room along the chain.
            src = chain_of_rooms[len(rooms) % len(chain_of_rooms)]

        free_exits = [k for k, v in src.exits.items() if v.dest is None]
        if len(free_exits) == 0:
            continue

        dest = M.new_room()
        src_exit = rng_map.choice(free_exits)
        dest_exit = reverse_direction(src_exit)
        M.connect(src.exits[src_exit], dest.exits[dest_exit])
        rooms.append(dest)

    # Generate the quest thats by collecting the coin.
    walkthrough.append("take coin")
    M.set_quest_from_commands(walkthrough)

    game = M.build()

    return game, metadata


class CoinCollectorLevel(gym.Env):
    """ Environment for the Coin Collector benchmark.

    Level difficulties are defined as follow:
      Level 1 to N: Nb. rooms = level, quest length = level

    """
    metadata = {'render.modes': ['human', 'ansi']}

    def __init__(self, level, n_games, game_generator_seed, grammar_flags={},
                 request_infos=[]):
        """
        Parameters
        ----------
        level : int
            Difficulty level of the generated games.
        n_games : int,
            Number of different games to generate.
        game_generator_seed : int
            Seed for the random generator used in the game generation process.
        grammar_flags : dict, optional
            Options for the grammar.
        request_infos : list of str, optional
            Specify which additional information of the `GameState` object
            should be available in the `infos` dictionary returned by
            `env.reset()` and `env.step()`. Possible choices are
            ["description", "inventory", "admissible_commands",
             "intermediate_reward"]
        """
        self.level = level
        self.n_games = n_games
        self.grammar_flags = grammar_flags
        self.game_generator_seed = game_generator_seed
        self.request_infos = request_infos
        self.current_game = None
        self.textworld_env = None
        self.seed(1234)

        # Get vocabulary
        vocab = textworld.text_utils.extract_vocab(self.grammar_flags)
        vocab += ["coin"]  # Additional words for this task.
        # To be compatible with existing general frameworks like OpenAI's baselines.
        self.action_space = text_spaces.Word(max_length=8, vocab=vocab)
        self.observation_space = text_spaces.Word(max_length=200, vocab=vocab)

        self.last_command = None
        self.textworld_env = None
        self.infos = None

    def _get_seeds_per_game(self):
        seeds_per_game = []
        for i in range(self.n_games):
            seeds = {}
            seeds["seed_map"] = self.rng_make.randint(65635)  # Shuffle map
            seeds["seed_objects"] = self.rng_make.randint(65635)  # Shuffle objects
            seeds["seed_quest"] = self.rng_make.randint(65635)  # Shuffle quest
            seeds["seed_grammar"] = self.rng_make.randint(65635)  # Shuffle grammar
            seeds["seed_inform7"] = self.seed_inform7
            seeds_per_game.append(seeds)

        return seeds_per_game

    def _make_game(self, seeds):
        game, metadata = make_coin_collector_game_from_level(self.level, self.grammar_flags, seeds)
        hashid = encode_seeds([self.game_generator_seed, self.level] + [seeds[k] for k in sorted(seeds)])
        game_name = "{}_{}".format(self.spec.id, hashid)
        game_file = textworld.generator.compile_game(game, game_name,
                                                     games_folder="gen_games/{}".format(self.spec.id))

        return game_file

    def _next_game(self):
        seeds = next(self._game_seeds_iterator)

        if seeds not in self.games_collection:
            self.games_collection[seeds] = self._make_game(dict(seeds))

        # Initialize random generator used to shuffle commands at each step.
        self.rng_cmds = np.random.RandomState(self.seed_cmds)
        return self.games_collection[seeds]

    def seed(self, seed=None):
        self.rng_games = np.random.RandomState(self.game_generator_seed + 1)  # To shuffle games.
        self.rng_make = np.random.RandomState(self.game_generator_seed + 2)  # To generate games.
        self.seed_cmds = self.game_generator_seed + 3  # To shuffle admissible commands.

        # Fixed seeds, for things that shouldn't vary across the games.
        self.seed_map = self.rng_make.randint(65635)
        self.seed_objects = self.rng_make.randint(65635)
        self.seed_quest = self.rng_make.randint(65635)
        self.seed_grammar = self.rng_make.randint(65635)
        self.seed_inform7 = self.rng_make.randint(65635)

        # Seeds per game, for things that should vary across games.
        seeds_per_game = self._get_seeds_per_game()
        self.seeds_per_game = [frozenset(seeds.items()) for seeds in seeds_per_game]

        # We shuffle the order in which the game will be seen.
        rng = np.random.RandomState(seed)
        rng.shuffle(self.seeds_per_game)

        # Prepare iterator used for looping through the games.
        self.games_collection = {}
        self._game_seeds_iterator = make_infinite_shuffled_iterator(self.seeds_per_game,
                                                                    rng=self.rng_games)

        return [seed]

    def reset(self):
        self.current_game = self._next_game()
        self.infos = {}
        self.infos["game_file"] = os.path.basename(self.current_game)
        with open(self.current_game.replace(".ulx", ".meta")) as f:
            self.infos["metadata"] = json.load(f)

        if self.textworld_env is not None:
            self.textworld_env.close()

        self.textworld_env = textworld.start(self.current_game)

        if "admissible_commands" in self.request_infos:
            self.textworld_env.activate_state_tracking()

        if "intermediate_reward" in self.request_infos:
            self.textworld_env.activate_state_tracking()
            self.textworld_env.compute_intermediate_reward()

        self.infos["directions_names"] = self.textworld_env.game.directions_names
        self.infos["verbs"] = self.textworld_env.game.verbs
        self.infos["objects_names"] = self.textworld_env.game.objects_names
        self.infos["objects_types"] = self.textworld_env.game.objects_types
        self.infos["objects_names_and_types"] = self.textworld_env.game.objects_names_and_types
        self.infos["max_score"] = 1

        self.performed_actions = set()
        self.game_state = self.textworld_env.reset()
        ob = self.game_state.feedback
        self._update_requested_infos()
        return ob, self.infos

    def _update_requested_infos(self):
        # Make sure requested infos are available.
        for attr in self.request_infos:
            # The following will take care of retrieving the information
            # from the game interpreter if needed.
            self.infos[attr] = getattr(self.game_state, attr)

    def skip(self, ngames=1):
        for i in range(ngames):
            next(self._game_seeds_iterator)

    def step(self, action):
        self.last_command = action
        self.game_state, reward, done = self.textworld_env.step(self.last_command)
        ob = self.game_state.feedback
        self._update_requested_infos()
        return ob, reward, done, self.infos

    def render(self, mode='human'):
        outfile = StringIO() if mode == 'ansi' else sys.stdout

        if self.last_command is not None:
            command = colorize("> " + self.last_command, "yellow", highlight=False)
            outfile.write(command + "\n\n")

        outfile.write(self.game_state.feedback + "\n")

        if mode != 'human':
            return outfile

    def close(self):
        if self.textworld_env is not None:
            self.textworld_env.close()

        self.textworld_env = None
