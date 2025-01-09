# coding: utf-8
#
# user-graph need to be generated by the following script
# tools/generate-u-u-matrix.py
import os
import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GATConv
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.utils import remove_self_loops, add_self_loops, degree
import torch_geometric

from common.abstract_recommender import GeneralRecommender
from common.loss import BPRLoss, EmbLoss
from common.init import xavier_uniform_initialization
from torch.nn import MultiheadAttention

class MENTOR(GeneralRecommender):
    def __init__(self, config, dataset):
        super(MENTOR, self).__init__(config, dataset)

        num_user = self.n_users
        num_item = self.n_items
        batch_size = config['train_batch_size']  # not used
        dim_x = config['embedding_size']
        self.feat_embed_dim = config['feat_embed_dim']
        self.n_layers = config['n_mm_layers']
        self.knn_k = config['knn_k']
        self.mm_image_weight = config['mm_image_weight']

        self.batch_size = batch_size
        self.num_user = num_user
        self.num_item = num_item
        self.k = 40
        self.aggr_mode = 'add'
        self.dataset = dataset
        self.dropout = config['dropout']
        # self.construction = 'weighted_max'
        self.reg_weight = config['reg_weight']
        self.align_weight = config['align_weight']
        self.mask_weight_g = config['mask_weight_g']
        self.mask_weight_f = config['mask_weight_f']
        self.temp = config['temp']
        self.drop_rate = 0.1

        # rep=>表示representation
        self.v_rep = None
        self.t_rep = None
        self.v_preference = None
        self.t_preference = None
        self.id_preference = None
        self.dim_latent = 64
        self.dim_feat = 128
        self.mm_adj = None

        self.mlp = nn.Linear(2*dim_x, 2*dim_x)

        dataset_path = os.path.abspath(config['data_path'] + config['dataset'])
        self.user_graph_dict = np.load(os.path.join(dataset_path, config['user_graph_dict_file']),
                                       allow_pickle=True).item()

        mm_adj_file = os.path.join(dataset_path, 'mm_adj_{}.pt'.format(self.knn_k))

        if self.v_feat is not None:
            self.image_embedding = nn.Embedding.from_pretrained(self.v_feat, freeze=False)
            self.image_trs = nn.Linear(self.v_feat.shape[1], self.feat_embed_dim)
        if self.t_feat is not None:
            self.text_embedding = nn.Embedding.from_pretrained(self.t_feat, freeze=False)
            self.text_trs = nn.Linear(self.t_feat.shape[1], self.feat_embed_dim)

        if os.path.exists(mm_adj_file):
            self.mm_adj = torch.load(mm_adj_file)
        else:
            if self.v_feat is not None:
                # 通过knn计算图的邻接矩阵
                indices, image_adj = self.get_knn_adj_mat(self.image_embedding.weight.detach())
                self.mm_adj = image_adj
            if self.t_feat is not None:
                indices, text_adj = self.get_knn_adj_mat(self.text_embedding.weight.detach())
                self.mm_adj = text_adj
            if self.v_feat is not None and self.t_feat is not None:
                # 视觉和文本模态特征的融合，参数可调整，这里设定的是0.1 可以进行调整为0.2
                self.mm_adj = self.mm_image_weight * image_adj + (1.0 - self.mm_image_weight) * text_adj

                del text_adj
                del image_adj
            torch.save(self.mm_adj, mm_adj_file)

        # 新增1：多头注意力机制相关的初始化
        # self.num_heads = 4  # 可从配置中获取头的数量，默认为4
        # self.dropout_attn = 0.1  # 注意力机制的 dropout 概率，默认为0.1
        # self.attention_layer = MultiheadAttention(embed_dim=self.feat_embed_dim, num_heads=self.num_heads,
        #                                         dropout=self.dropout_attn)

        # packing interaction in training into edge_index
        train_interactions = dataset.inter_matrix(form='coo').astype(np.float32)
        edge_index = self.pack_edge_index(train_interactions)
        self.edge_index = torch.tensor(edge_index, dtype=torch.long).t().contiguous().to(self.device)
        self.edge_index = torch.cat((self.edge_index, self.edge_index[[1, 0]]), dim=1)

        # pdb.set_trace()
        # 用户与物品权重的初始化矩阵为2x1
        self.weight_u = nn.Parameter(nn.init.xavier_normal_(
            torch.tensor(np.random.randn(self.num_user, 2, 1), dtype=torch.float32, requires_grad=True)))
        self.weight_u.data = F.softmax(self.weight_u, dim=1)

        self.weight_i = nn.Parameter(nn.init.xavier_normal_(
            torch.tensor(np.random.randn(self.num_item, 2, 1), dtype=torch.float32, requires_grad=True)))
        self.weight_i.data = F.softmax(self.weight_i, dim=1)

        self.item_index = torch.zeros([self.num_item], dtype=torch.long)
        index = []
        for i in range(self.num_item):
            self.item_index[i] = i
            index.append(i)
        self.drop_percent = self.drop_rate
        self.single_percent = 1
        self.double_percent = 0

        # 生成部分节点的屏蔽索引，随即划分成单模态屏蔽
        drop_item = torch.tensor(
            np.random.choice(self.item_index, int(self.num_item * self.drop_percent), replace=False))
        drop_item_single = drop_item[:int(self.single_percent * len(drop_item))]

        self.dropv_node_idx_single = drop_item_single[:int(len(drop_item_single) * 1 / 3)]
        self.dropt_node_idx_single = drop_item_single[int(len(drop_item_single) * 2 / 3):]

        self.dropv_node_idx = self.dropv_node_idx_single
        self.dropt_node_idx = self.dropt_node_idx_single

        mask_cnt = torch.zeros(self.num_item, dtype=int).tolist()
        for edge in edge_index:
            mask_cnt[edge[1] - self.num_user] += 1
        mask_dropv = []
        mask_dropt = []
        for idx, num in enumerate(mask_cnt):
            temp_false = [False] * num
            temp_true = [True] * num
            mask_dropv.extend(temp_false) if idx in self.dropv_node_idx else mask_dropv.extend(temp_true)
            mask_dropt.extend(temp_false) if idx in self.dropt_node_idx else mask_dropt.extend(temp_true)

        edge_index = edge_index[np.lexsort(edge_index.T[1, None])]
        edge_index_dropv = edge_index[mask_dropv]
        edge_index_dropt = edge_index[mask_dropt]

        self.edge_index_dropv = torch.tensor(edge_index_dropv).t().contiguous().to(self.device)
        self.edge_index_dropt = torch.tensor(edge_index_dropt).t().contiguous().to(self.device)

        self.edge_index_dropv = torch.cat((self.edge_index_dropv, self.edge_index_dropv[[1, 0]]), dim=1)
        self.edge_index_dropt = torch.cat((self.edge_index_dropt, self.edge_index_dropt[[1, 0]]), dim=1)

        #简单的全连接层对用户-物品特征进行映射
        self.MLP_user = nn.Linear(self.dim_latent * 2, self.dim_latent)

        # 多模态表示学习，视觉，文本，id
        if self.v_feat is not None:
            self.v_gcn = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode, dim_latent=64,
                             device=self.device, features=self.v_feat)
            self.v_gcn_n1 = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode, dim_latent=64,
                             device=self.device, features=self.v_feat)
            self.v_gcn_n2 = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode, dim_latent=64,
                                device=self.device, features=self.v_feat)
        if self.t_feat is not None:
            self.t_gcn = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode, dim_latent=64,
                             device=self.device, features=self.t_feat)
            self.t_gcn_n1 = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode, dim_latent=64,
                             device=self.device, features=self.t_feat)
            self.t_gcn_n2 = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode, dim_latent=64,
                                device=self.device, features=self.t_feat)

        self.id_feat = nn.Parameter(
            nn.init.xavier_normal_(torch.tensor(np.random.randn(self.n_items, self.dim_latent), dtype=torch.float32,
                                                requires_grad=True), gain=1).to(self.device))
        self.id_gcn = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode,
                          dim_latent=64, device=self.device, features=self.id_feat)

        # 总的融合嵌入
        self.result_embed = nn.Parameter(
            nn.init.xavier_normal_(torch.tensor(np.random.randn(num_user + num_item, dim_x)))).to(self.device)
        # 模态引导的嵌入
        self.result_embed_guide = nn.Parameter(
            nn.init.xavier_normal_(torch.tensor(np.random.randn(num_user + num_item, dim_x)))).to(self.device)
        # 单模态的嵌入
        self.result_embed_v = nn.Parameter(
            nn.init.xavier_normal_(torch.tensor(np.random.randn(num_user + num_item, dim_x)))).to(self.device)
        self.result_embed_t = nn.Parameter(
            nn.init.xavier_normal_(torch.tensor(np.random.randn(num_user + num_item, dim_x)))).to(self.device)
        # 多层的嵌入
        self.result_embed_n1 = nn.Parameter(
            nn.init.xavier_normal_(torch.tensor(np.random.randn(num_user + num_item, dim_x)))).to(self.device)
        self.result_embed_n2 = nn.Parameter(
            nn.init.xavier_normal_(torch.tensor(np.random.randn(num_user + num_item, dim_x)))).to(self.device)

    def get_knn_adj_mat(self, mm_embeddings):
        context_norm = mm_embeddings.div(torch.norm(mm_embeddings, p=2, dim=-1, keepdim=True))
        sim = torch.mm(context_norm, context_norm.transpose(1, 0))
        _, knn_ind = torch.topk(sim, self.knn_k, dim=-1)
        adj_size = sim.size()
        del sim
        # construct sparse adj
        indices0 = torch.arange(knn_ind.shape[0]).to(self.device)
        indices0 = torch.unsqueeze(indices0, 1)
        indices0 = indices0.expand(-1, self.knn_k)
        indices = torch.stack((torch.flatten(indices0), torch.flatten(knn_ind)), 0)
        # norm
        return indices, self.compute_normalized_laplacian(indices, adj_size)

    def compute_normalized_laplacian(self, indices, adj_size):
        adj = torch.sparse.FloatTensor(indices, torch.ones_like(indices[0]), adj_size)
        row_sum = 1e-7 + torch.sparse.sum(adj, -1).to_dense()
        r_inv_sqrt = torch.pow(row_sum, -0.5)
        rows_inv_sqrt = r_inv_sqrt[indices[0]]
        cols_inv_sqrt = r_inv_sqrt[indices[1]]
        values = rows_inv_sqrt * cols_inv_sqrt
        return torch.sparse.FloatTensor(indices, values, adj_size)

    def pre_epoch_processing(self):
        self.epoch_user_graph, self.user_weight_matrix = self.topk_sample(self.k)
        self.user_weight_matrix = self.user_weight_matrix.to(self.device)

    def pack_edge_index(self, inter_mat):
        rows = inter_mat.row
        cols = inter_mat.col + self.n_users
        # ndarray([598918, 2]) for ml-imdb
        return np.column_stack((rows, cols))

    def InfoNCE(self, view1, view2, temp):
        view1, view2 = F.normalize(view1, dim=1), F.normalize(view2, dim=1)
        pos_score = (view1 * view2).sum(dim=-1)
        pos_score = torch.exp(pos_score / temp)
        ttl_score = torch.matmul(view1, view2.transpose(0, 1))
        ttl_score = torch.exp(ttl_score / temp).sum(dim=1)
        cl_loss = -torch.log(pos_score / ttl_score)
        return torch.mean(cl_loss)

    def forward(self, interaction):

        # 正负样本信息
        user_nodes, pos_item_nodes, neg_item_nodes = interaction[0], interaction[1], interaction[2]
        pos_item_nodes += self.n_users
        neg_item_nodes += self.n_users

        # GCN for id, v, t modalities
        self.v_rep, self.v_preference = self.v_gcn(self.edge_index_dropv, self.edge_index, self.v_feat)
        self.t_rep, self.t_preference = self.t_gcn(self.edge_index_dropt, self.edge_index, self.t_feat)
        self.id_rep, self.id_preference = self.id_gcn(self.edge_index_dropt, self.edge_index, self.id_feat)

        # 引入的随机噪声进行扰动
        # random noise GCN for v and t
        self.v_rep_n1, _ = self.v_gcn_n1(self.edge_index_dropv, self.edge_index, self.v_feat, perturbed=True)
        self.t_rep_n1, _ = self.t_gcn_n1(self.edge_index_dropt, self.edge_index, self.t_feat, perturbed=True)
        self.v_rep_n2, _ = self.v_gcn_n2(self.edge_index_dropv, self.edge_index, self.v_feat, perturbed=True)
        self.t_rep_n2, _ = self.t_gcn_n2(self.edge_index_dropt, self.edge_index, self.t_feat, perturbed=True)

        # v, t, id, and vt modalities
        representation = torch.cat((self.v_rep, self.t_rep), dim=1)
        guide_representation = torch.cat((self.id_rep, self.id_rep), dim=1)
        v_representation = torch.cat((self.v_rep, self.v_rep), dim=1)
        t_representation = torch.cat((self.t_rep, self.t_rep), dim=1)

        # noise rep
        representation_n1 = torch.cat((self.v_rep_n1, self.t_rep_n1), dim=1)
        representation_n2 = torch.cat((self.v_rep_n2, self.t_rep_n2), dim=1)

        # 维度的调整
        self.v_rep = torch.unsqueeze(self.v_rep, 2)
        self.t_rep = torch.unsqueeze(self.t_rep, 2)
        self.id_rep = torch.unsqueeze(self.id_rep, 2)

        # 用户向量表示，使用权重weight_u进行调整
        user_rep = torch.cat((self.v_rep[:self.num_user], self.t_rep[:self.num_user]), dim=2)
        user_rep = self.weight_u.transpose(1, 2) * user_rep
        user_rep = torch.cat((user_rep[:, :, 0], user_rep[:, :, 1]), dim=1)

        # # 新增1：多头注意力机制
        # # 视觉和文本模态特征的融合，采用多头注意力机制进行融合
        # v_embed = self.image_trs(self.image_embedding.weight.detach())
        # t_embed = self.text_trs(self.text_embedding.weight.detach())

        # # 为了适应多头注意力机制的输入格式，需要对嵌入表示进行维度调整
        # v_embed = v_embed.unsqueeze(0)  # 增加一个批次维度，形状变为 [1, num_nodes, feat_embed_dim]
        # t_embed = t_embed.unsqueeze(0)

        # # 通过多头注意力机制得到融合后的特征表示
        # attn_output, attn_weights = self.attention_layer(v_embed, t_embed, t_embed)

        # # 去除批次维度，恢复原始形状
        # attn_output = attn_output.squeeze(0)

        # # 根据注意力权重计算视觉和文本模态在融合中的贡献度
        # v_contribution = attn_weights[:, :, 0].mean(dim=1)  # 计算每个节点上视觉模态的平均注意力权重
        # t_contribution = attn_weights[:, :, 1].mean(dim=1)  # 计算每个节点上文本模态的平均注意力权重

        # # 使用贡献度来融合邻接矩阵
        # self.mm_adj = v_contribution.unsqueeze(1) * image_adj + t_contribution.unsqueeze(1) * text_adj

        # 引导用户向量表示
        guide_user_rep = torch.cat((self.id_rep[:self.num_user], self.id_rep[:self.num_user]), dim=2)
        guide_user_rep = self.weight_u.transpose(1, 2) * guide_user_rep
        guide_user_rep = torch.cat((guide_user_rep[:, :, 0], guide_user_rep[:, :, 1]), dim=1)

        # v用户向量表示
        v_user_rep = torch.cat((self.v_rep[:self.num_user], self.v_rep[:self.num_user]), dim=2)
        v_user_rep = self.weight_u.transpose(1, 2) * v_user_rep
        v_user_rep = torch.cat((v_user_rep[:, :, 0], v_user_rep[:, :, 1]), dim=1)

        # t用户向量表示
        t_user_rep = torch.cat((self.t_rep[:self.num_user], self.t_rep[:self.num_user]), dim=2)
        t_user_rep = self.weight_u.transpose(1, 2) * t_user_rep
        t_user_rep = torch.cat((t_user_rep[:, :, 0], t_user_rep[:, :, 1]), dim=1)

        # 噪声的用户向量表示
        # noise rep1
        self.v_rep_n1 = torch.unsqueeze(self.v_rep_n1, 2)
        self.t_rep_n1 = torch.unsqueeze(self.t_rep_n1, 2)
        user_rep_n1 = torch.cat((self.v_rep_n1[:self.num_user], self.t_rep_n1[:self.num_user]), dim=2)
        user_rep_n1 = self.weight_u.transpose(1, 2) * user_rep_n1
        user_rep_n1 = torch.cat((user_rep_n1[:, :, 0], user_rep_n1[:, :, 1]), dim=1)

        # noise rep2
        self.v_rep_n2 = torch.unsqueeze(self.v_rep_n2, 2)
        self.t_rep_n2 = torch.unsqueeze(self.t_rep_n2, 2)
        user_rep_n2 = torch.cat((self.v_rep_n2[:self.num_user], self.t_rep_n2[:self.num_user]), dim=2)
        user_rep_n2 = self.weight_u.transpose(1, 2) * user_rep_n2
        user_rep_n2 = torch.cat((user_rep_n2[:, :, 0], user_rep_n2[:, :, 1]), dim=1)

        # item 物品相关的表示
        item_rep = representation[self.num_user:]
        item_rep_n1 = representation_n1[self.num_user:]
        item_rep_n2 = representation_n2[self.num_user:]


        # 引导物品向量表示
        guide_item_rep = guide_representation[self.num_user:]
        v_item_rep = v_representation[self.num_user:]
        t_item_rep = t_representation[self.num_user:]

        # build item-item graph 项目项目图能够捕捉语义之间的联系
        h = self.buildItemGraph(item_rep)
        h_guide = self.buildItemGraph(guide_item_rep)
        h_v = self.buildItemGraph(v_item_rep)
        h_t = self.buildItemGraph(t_item_rep)
        h_n1 = self.buildItemGraph(item_rep_n1)
        h_n2 = self.buildItemGraph(item_rep_n2)

        user_rep = user_rep
        item_rep = item_rep + h

        item_rep_n1 = item_rep_n1 + h_n1
        item_rep_n2 = item_rep_n2 + h_n2

        guide_item_rep = guide_item_rep + h_guide
        v_item_rep = v_item_rep + h_v
        t_item_rep = t_item_rep + h_t

        # build result embedding
        self.user_rep = user_rep
        self.item_rep = item_rep
        self.result_embed = torch.cat((user_rep, item_rep), dim=0)

        self.guide_user_rep = guide_user_rep
        self.guide_item_rep = guide_item_rep
        self.result_embed_guide = torch.cat((guide_user_rep, guide_item_rep), dim=0)

        self.v_user_rep = v_user_rep
        self.v_item_rep = v_item_rep
        self.result_embed_v = torch.cat((v_user_rep, v_item_rep), dim=0)

        self.t_user_rep = t_user_rep
        self.t_item_rep = t_item_rep
        self.result_embed_t = torch.cat((t_user_rep, t_item_rep), dim=0)

        self.user_rep_n1 = user_rep_n1
        self.item_rep_n1 = item_rep_n1
        self.result_embed_n1 = torch.cat((user_rep_n1, item_rep_n1), dim=0)

        self.user_rep_n2 = user_rep_n2
        self.item_rep_n2 = item_rep_n2
        self.result_embed_n2 = torch.cat((user_rep_n2, item_rep_n2), dim=0)

        # calculate pos and neg scores
        user_tensor = self.result_embed[user_nodes]
        pos_item_tensor = self.result_embed[pos_item_nodes]
        neg_item_tensor = self.result_embed[neg_item_nodes]
        pos_scores = torch.sum(user_tensor * pos_item_tensor, dim=1)
        neg_scores = torch.sum(user_tensor * neg_item_tensor, dim=1)
        return pos_scores, neg_scores

    def buildItemGraph(self, h):
        for i in range(self.n_layers):
            h = torch.sparse.mm(self.mm_adj, h)
        return h

    def fit_Gaussian_dis(self):
        # 代表不同模态对齐的分布，接下来要计算距离损失
        r_var = torch.var(self.result_embed)
        r_mean = torch.mean(self.result_embed)
        g_var = torch.var(self.result_embed_guide)
        g_mean = torch.mean(self.result_embed_guide)
        v_var = torch.var(self.result_embed_v)
        v_mean = torch.mean(self.result_embed_v)
        t_var = torch.var(self.result_embed_t)
        t_mean = torch.mean(self.result_embed_t)
        return r_var, r_mean, g_var, g_mean, v_var, v_mean, t_var, t_mean

    def calculate_loss(self, interaction):
        user = interaction[0]
        pos_scores, neg_scores = self.forward(interaction)

        # BPR loss 贝叶斯个性化排名损失，利用正负样本的计算
        loss_value = -torch.mean(torch.log2(torch.sigmoid(pos_scores - neg_scores)))

        # 正则化的损失,主要是对用户和物品的embedding进行正则化
        # reg
        reg_embedding_loss_v = (self.v_preference[user] ** 2).mean() if self.v_preference is not None else 0.0
        reg_embedding_loss_t = (self.t_preference[user] ** 2).mean() if self.t_preference is not None else 0.0
        reg_loss = self.reg_weight * (reg_embedding_loss_v + reg_embedding_loss_t)
        reg_loss += self.reg_weight * (self.weight_u ** 2).mean()

        # 掩码一致性损失，超参数mask_weight_f的调整
        # mask
        with torch.no_grad():
            u_temp, i_temp = self.user_rep.clone(), self.item_rep.clone()
            u_temp2, i_temp2 = self.user_rep.clone(), self.item_rep.clone()
            u_temp.detach()
            i_temp.detach()
            u_temp2.detach()
            i_temp2.detach()
            u_temp2 = self.mlp(u_temp2)
            i_temp2 = self.mlp(i_temp2)
            u_temp = F.dropout(u_temp, self.dropout)
            i_temp = F.dropout(i_temp, self.dropout)
        mask_loss_u = 1 - F.cosine_similarity(u_temp, u_temp2).mean()
        mask_loss_i = 1 - F.cosine_similarity(i_temp, i_temp2).mean()
        mask_f_loss = self.mask_weight_f * (mask_loss_i + mask_loss_u)

        # 对齐损失，超参数align_weight的调整
        # guide 粗粒度-分布
        r_var, r_mean, g_var, g_mean, v_var, v_mean, t_var, t_mean = self.fit_Gaussian_dis()
        # # id and v+t
        # dis_loss_i_vt = (torch.abs(g_var - r_var) +
        #                  torch.abs(g_mean - r_mean)).mean()
        #
        # # id and v 
        # dis_loss_i_v = (torch.abs(g_var - v_var) +
        #                 torch.abs(g_mean - v_mean)).mean()
        # # id and t
        # dis_loss_i_t = (torch.abs(g_var - t_var) +
        #                 torch.abs(g_mean - t_mean)).mean()
        #
        # # v and v+t
        # dis_loss_v_vt = (torch.abs(r_var - v_var) +
        #                  torch.abs(r_mean - v_mean)).mean()
        #
        # # t and v+t
        # dis_loss_t_vt = (torch.abs(r_var - t_var) +
        #                  torch.abs(r_mean - t_mean)).mean()
        #
        # # # v and t 
        # dis_loss_v_t = (torch.abs(v_var - t_var) +
        #                 torch.abs(v_mean - t_mean)).mean()


        # # # total 
        # # dis_loss = (dis_loss_i_vt + dis_loss_i_v + dis_loss_i_t
        # #             + dis_loss_v_vt + dis_loss_t_vt
        # #             + dis_loss_v_t)

        # # level4
        # # dis_loss = (dis_loss_v_t)
        # # level3
        # # dis_loss = (dis_loss_v_vt + dis_loss_t_vt)
        # # level2
        # # dis_loss = (dis_loss_i_v + dis_loss_i_t)
        # # level1
        # # dis_loss = dis_loss_i_vt

        align_loss = ((torch.abs(g_var - r_var) +
                         torch.abs(g_mean - r_mean)).mean() +
                        (torch.abs(g_var - v_var) +
                         torch.abs(g_mean - v_mean)).mean() +
                       (torch.abs(g_var - t_var) +
                        torch.abs(g_mean - t_mean)).mean() +
                      (torch.abs(r_var - v_var) +
                       torch.abs(r_mean - v_mean)).mean() +
                     (torch.abs(r_var - t_var) +
                      torch.abs(r_mean - t_mean)).mean() +
                    (torch.abs(v_var - t_var) +
                     torch.abs(v_mean - t_mean)).mean())

        align_loss = align_loss * self.align_weight

        # 图噪音cl 图扰动的噪声损失，超参数mask_weight_g的调整
        # inspired by SimGCL
        mask_g_loss = (self.InfoNCE(self.result_embed_n1[:self.n_users], self.result_embed_n2[:self.n_users], self.temp)
                       + self.InfoNCE(self.result_embed_n1[self.n_users:], self.result_embed_n2[self.n_users:], self.temp))

        mask_g_loss = mask_g_loss * self.mask_weight_g

        # loss_value 是BPR损失，reg_loss是正则化损失，align_loss是对齐损失，mask_f_loss是掩码损失，mask_g_loss是图噪音cl损失，
        return loss_value + reg_loss + align_loss + mask_f_loss + mask_g_loss

    def full_sort_predict(self, interaction):
        user_tensor = self.result_embed[:self.n_users]
        item_tensor = self.result_embed[self.n_users:]

        temp_user_tensor = user_tensor[interaction[0], :]
        score_matrix = torch.matmul(temp_user_tensor, item_tensor.t())
        return score_matrix

    def topk_sample(self, k):
        user_graph_index = []
        count_num = 0
        user_weight_matrix = torch.zeros(len(self.user_graph_dict), k)
        tasike = []
        for i in range(k):
            tasike.append(0)
        for i in range(len(self.user_graph_dict)):
            if len(self.user_graph_dict[i][0]) < k:
                count_num += 1
                if len(self.user_graph_dict[i][0]) == 0:
                    # pdb.set_trace()
                    user_graph_index.append(tasike)
                    continue
                user_graph_sample = self.user_graph_dict[i][0][:k]
                user_graph_weight = self.user_graph_dict[i][1][:k]
                while len(user_graph_sample) < k:
                    rand_index = np.random.randint(0, len(user_graph_sample))
                    user_graph_sample.append(user_graph_sample[rand_index])
                    user_graph_weight.append(user_graph_weight[rand_index])
                user_graph_index.append(user_graph_sample)

                user_weight_matrix[i] = F.softmax(torch.tensor(user_graph_weight), dim=0)  # softmax
                continue
            user_graph_sample = self.user_graph_dict[i][0][:k]
            user_graph_weight = self.user_graph_dict[i][1][:k]

            user_weight_matrix[i] = F.softmax(torch.tensor(user_graph_weight), dim=0)  # softmax
            user_graph_index.append(user_graph_sample)

        # pdb.set_trace()
        return user_graph_index, user_weight_matrix

    def print_embd(self):
        return self.result_embed_v, self.result_embed_t


class GCN(torch.nn.Module):
    def __init__(self, datasets, batch_size, num_user, num_item, dim_id, aggr_mode,
                 dim_latent=None, device=None, features=None):
        super(GCN, self).__init__()
        self.batch_size = batch_size
        self.num_user = num_user
        self.num_item = num_item
        self.datasets = datasets
        self.dim_id = dim_id
        self.dim_feat = features.size(1)
        self.dim_latent = dim_latent
        self.aggr_mode = aggr_mode
        self.device = device

        if self.dim_latent:
            self.preference = nn.Parameter(nn.init.xavier_normal_(torch.tensor(
                np.random.randn(num_user, self.dim_latent), dtype=torch.float32, requires_grad=True),
                gain=1).to(self.device))
            self.MLP = nn.Linear(self.dim_feat, 4 * self.dim_latent)
            self.MLP_1 = nn.Linear(4 * self.dim_latent, self.dim_latent)
            self.conv_embed_1 = Base_gcn(self.dim_latent, self.dim_latent, aggr=self.aggr_mode)


        else:
            self.preference = nn.Parameter(nn.init.xavier_normal_(torch.tensor(
                np.random.randn(num_user, self.dim_feat), dtype=torch.float32, requires_grad=True),
                gain=1).to(self.device))
            self.conv_embed_1 = Base_gcn(self.dim_latent, self.dim_latent, aggr=self.aggr_mode)

    def forward(self, edge_index_drop, edge_index, features, perturbed=False):
        temp_features = self.MLP_1(F.leaky_relu(self.MLP(features))) if self.dim_latent else features
        x = torch.cat((self.preference, temp_features), dim=0).to(self.device)
        x = F.normalize(x).to(self.device)

        h = self.conv_embed_1(x, edge_index)

        if perturbed:
            random_noise = torch.rand_like(h).cuda()
            h += torch.sign(h) * F.normalize(random_noise, dim=-1) * 0.1
        h_1 = self.conv_embed_1(h, edge_index)

        if perturbed:
            random_noise = torch.rand_like(h).cuda()
            h_1 += torch.sign(h_1) * F.normalize(random_noise, dim=-1) * 0.1
        # h_2 = self.conv_embed_1(h_1, edge_index)

        x_hat = x + h + h_1
        return x_hat, self.preference


class Base_gcn(MessagePassing):
    def __init__(self, in_channels, out_channels, normalize=True, bias=True, aggr='add', **kwargs):
        super(Base_gcn, self).__init__(aggr=aggr, **kwargs)
        self.aggr = aggr
        self.in_channels = in_channels
        self.out_channels = out_channels

    def forward(self, x, edge_index, size=None):
        # pdb.set_trace()
        if size is None:
            edge_index, _ = remove_self_loops(edge_index)
            # edge_index, _ = add_self_loops(edge_index, num_nodes=x.size(0))
        x = x.unsqueeze(-1) if x.dim() == 1 else x
        # pdb.set_trace()
        return self.propagate(edge_index, size=(x.size(0), x.size(0)), x=x)

    def message(self, x_j, edge_index, size):
        if self.aggr == 'add':
            # pdb.set_trace()
            row, col = edge_index
            deg = degree(row, size[0], dtype=x_j.dtype)
            deg_inv_sqrt = deg.pow(-0.5)
            norm = deg_inv_sqrt[row] * deg_inv_sqrt[col]
            return norm.view(-1, 1) * x_j
        return x_j

    def update(self, aggr_out):
        return aggr_out

    def __repr(self):
        return '{}({},{})'.format(self.__class__.__name__, self.in_channels, self.out_channels)

