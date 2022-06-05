""" Import libraries Original """
import copy
from math import sqrt
import numpy as np
import torch
from Util import Util
import importlib
import config as CFG
importlib.reload(CFG)

device = torch.device(CFG.device)
util = Util(CFG)

class Node():
    """
    Node: Represents a game state
    """
    def __init__(self, state=None):
        self.states = None
        self.is_root = False
        self.child_nodes = []
        self.action = None
        self.actions = [0] * CFG.history_size # for one-hot
        self.player = CFG.first_player
        self.input_features = None

        if state:
            self.states = util.create_states(state)

        """ Edge """
        self.n = 0 # 訪問回数 (visit count)
        self.w = 0 # 累計行動価値 (total action-value)
        self.p = 0 # 事前確率 (prior probability)
        self.Q = 0 # 平均行動価値 (action-value)


class MCTS():
    """
    root node: 探索開始ノード
    """
    def __init__(self, env, model, train=True):
        self.env = copy.deepcopy(env)
        self.env.reset()
        self.model = model
        self.player = None
        self.train = train
        
        if not train:
            self.model.eval()

    def __call__(self, node, play_count=1):

        self.model.eval()
        self.player = node.player # Important!        
        root_node = copy.deepcopy(node)
        root_node.is_root = True

        """ シミュレーション """
        for i in range(CFG.num_simulation): # AlphaGo Zero 1600 sim / AlphaZero 800 sim
            self.env.reset()
            self.env.state = copy.deepcopy(root_node.states[0])
            self.env.player = root_node.player  # resetされたので、Self play でのプレーヤーに再設定
            
            """ ルートノードから再帰的に探索を実行 """
            self.search(root_node)

        node.input_features = root_node.input_features # Copy from simulated node. Necessary for dataset.

        next_node = self.play(root_node, play_count)

        return next_node

    def search(self, node):

        """ ゲームオーバー """
        if self.env.done:
            # v = abs(self.env.reward) # 0 or 1
            v = -self.env.reward # 0 or -1
            # print('Player', self.env.player, node.player)
            # print('Reward', self.env.reward)
            # print(self.env.state)
            # print(node.states[0])
            # print('------------------------------------')
            self.backup(node, v) # 相手の手番となるノード
            return -v

        """ リーフ """
        if len(node.child_nodes) == 0:
            v = self.expand(node)
            self.backup(node, v)
            return -v
 
        """ 選択 """
        next_node = self.select(node)
        self.env.step(next_node.action)

        """ 探索（相手の手番で） """
        v = self.search(next_node)

        """ バックアップ """ 
        self.backup(node, v) 

        return -v


    def select(self, node):
        """ 選択
        Ｑ（相手にとっては－Ｑ）＋Ｕの最大値から、最良の行動を選ぶ
        """
        pucts = [] # PUCTの値
        cpuct = CFG.puct # 1-6
        s = node.n - 1 # Σ_b (N(s,b)) と同じこと
        child_nodes = util.get_child_nodes(node) # エッジの取得

        if node.is_root:
            """ 事前確率にディリクレノイズを追加 """
            child_nodes = self.add_dirichlet_noise(child_nodes)

        for child_node in child_nodes:
            p = child_node.p
            n = child_node.n
            Q = child_node.Q
            U = cpuct * p * sqrt(s) / (1 + n)
            pucts.append(Q + U)
  
        max_index = np.argmax(pucts)
        next_node = node.child_nodes[max_index]

        return next_node


    """ 展開と評価 """
    def expand(self, node):

        """ 入力特徴の作成 """
        features = util.state2feature(node)

        """ 推論 """
        p, v = self.model(features)

        """ バッチの次元を削除 """
        p = p[0].tolist()
        v = v[0].tolist()[0] # スカラーに変換

        """ 子ノードの生成 """
        self.add_child_nodes(node, p)

        return v 

    def backup(self, node, v):
        """ バックアップ """
        node.n += 1
        node.w += v
        node.Q = node.w / node.n

    def play(self, node, play_count):
        """ 実行

        探索が完了すると、 N^(1/τ)に比例した探索確率πで行動を決定
        Nはルート状態からの各ノードへの訪問回数、
        τは温度を制御するパラメータ。

        τ: 温度パラメーター
            -∞: 決定的に選択
            1  : 確率的に選択
            ∞ : ランダムに選択
        """

        """ 温度パラメーター """
        if self.train:
            """ 訓練時は最初のｎ手までは確率的に """
            tau = 1 if play_count < CFG.tau_limit else 0
        else:
            """ 評価時には決定的に """
            tau = 0
                    
        N = []
        for child_node in node.child_nodes:
            N.append(child_node.n)

        if tau > 0:
            """ 確率的に選択 """
            N_pow = np.power(N, 1/tau)
            N_sum = np.sum(N_pow) + 1e-20
            pi = N_pow / N_sum
            p = np.random.choice(pi, p=pi)
            index = np.argwhere(pi==p)[0][0].tolist()
        else:
            """ 決定的に選択 """
            index = np.argmax(N)

        """ 次のノードへ遷移 """
        next_node = node.child_nodes[index]

        return next_node

    def add_dirichlet_noise(self, child_nodes):
        """
        ルートノードの事前確率にディリクレノイズを加えて、さらなる探索
        P(s, a) = (1 - ε) * p(a) + ε*η(a)
        where η～ Dir(0.03), ε= 0.25
        """
        e = CFG.Dirichlet_epsilon
        alpha = CFG.Dirichlet_alpha

        dirichlet_noise = np.random.dirichlet([alpha] * len(child_nodes))

        for i, child_node in enumerate(child_nodes):
            x = child_node.p
            p = (1-e) * x + e * dirichlet_noise[i]
            child_nodes[i].p = p

        return child_nodes

    """ 子ノードの生成 """
    def add_child_nodes(self, node, p):

        """ 合法手の取得 """
        legal_actions = self.env.get_legal_actions(node.states[0])

        for action in legal_actions:
            states = util.get_next_states(node.states, action, node.player)
            actions = util.get_next_actions(node.actions)

            child_node = Node()
            child_node.p = p[action]
            child_node.action = action
            child_node.actions = actions
            child_node.states = states
            child_node.player = -node.player
            node.child_nodes.append(child_node)
