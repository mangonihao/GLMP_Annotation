import torch
import torch.nn as nn
import torch.nn.functional as F
from utils.config import *
from utils.utils_general import _cuda


class ContextRNN(nn.Module):#Global Memory encoder的一个组件
    def __init__(self, input_size, hidden_size, dropout, n_layers=1):
        super(ContextRNN, self).__init__()      
        self.input_size = input_size #lang.n_words，映射中单词的个数
        self.hidden_size = hidden_size
        self.n_layers = n_layers     
        self.dropout = dropout  #将元素置为0的概率
        self.dropout_layer = nn.Dropout(dropout)
        self.embedding = nn.Embedding(input_size, hidden_size, padding_idx=PAD_token) #nn.Embedding:参数分别为（单词个数 词向量维度 遇到PAD_token输出0）
        self.gru = nn.GRU(hidden_size, hidden_size, n_layers, dropout=dropout, bidirectional=True)
        self.W = nn.Linear(2*hidden_size, hidden_size) #因为前面的双向编码

    def get_state(self, bsz):
        """Get cell states and hidden states."""
        return _cuda(torch.zeros(2, bsz, self.hidden_size))

    def forward(self, input_seqs, input_lengths, hidden=None):
        #input_seqs [66 8 4] 66是动态变化的，应该是每个故事长度不同导致的
        # Note: we run this all at once (over multiple batches of multiple sequences)
        #embedded = self.embedding(input_seqs)
        embedded = self.embedding(input_seqs.contiguous().view(input_seqs.size(0), -1).long())  #contiguous返回连续内存的数据
        #[66 32 128]
        embedded = embedded.view(input_seqs.size()+(embedded.size(-1),))  #这一步是干嘛[66 8 4 128]
        embedded = torch.sum(embedded, 2).squeeze(2) #[66 8 128]
        embedded = self.dropout_layer(embedded)#[66 8 128]
        hidden = self.get_state(input_seqs.size(1)) #[2 8 128]
        if input_lengths:
            embedded = nn.utils.rnn.pack_padded_sequence(embedded, input_lengths, batch_first=False)
        outputs, hidden = self.gru(embedded, hidden)
        #outputs [235 256]  (seq_len, batch, num_directions * hidden_size) hidden [2 8 128](num_layers * num_directions, batch, hidden_size)
        if input_lengths:
           outputs, _ = nn.utils.rnn.pad_packed_sequence(outputs, batch_first=False)   #[66 8 256]恢复正常
        hidden = self.W(torch.cat((hidden[0], hidden[1]), dim=1))  # .unsqueeze(0)
        outputs = self.W(outputs)
        return outputs.transpose(0,1), hidden #输出写入EK，最终隐含态用来query EK得到EK步骤的soft memory attention.


class ExternalKnowledge(nn.Module):
    def __init__(self, vocab, embedding_dim, hop, dropout):
        super(ExternalKnowledge, self).__init__()
        self.max_hops = hop
        self.embedding_dim = embedding_dim
        self.dropout = dropout
        self.dropout_layer = nn.Dropout(dropout) 
        for hop in range(self.max_hops+1):   #multi-hop机制？
            C = nn.Embedding(vocab, embedding_dim, padding_idx=PAD_token)
            C.weight.data.normal_(0, 0.1)
            self.add_module("C_{}".format(hop), C)
        self.C = AttrProxy(self, "C_")
        self.softmax = nn.Softmax(dim=1)
        self.sigmoid = nn.Sigmoid()
        #self.conv_layer = nn.Conv1d(embedding_dim, embedding_dim, 5, padding=2)

    def add_lm_embedding(self, full_memory, kb_len, conv_len, hiddens):
        for bi in range(full_memory.size(0)):
            start, end = kb_len[bi], kb_len[bi]+conv_len[bi]
            full_memory[bi, start:end, :] = full_memory[bi, start:end, :] + hiddens[bi, :conv_len[bi], :]
        return full_memory

    def load_memory(self, story, kb_len, conv_len, hidden, dh_outputs):  # 第二次改u
        # Forward multiple hop mechanism
        #u = [hidden.squeeze(0)]
        query = hidden
        story_size = story.size()
        self.m_story = []
        #训练时只运行一次，相当于EK的存储，用来存储不同hop的词袋表示，但是KB并没有显示的存储在这里，也就是说只对对话历史进行query
        #KB中知识的作用是用来将对话中单词的关系抽象出来
        for hop in range(self.max_hops):
            #t = story.contiguous().view(story_size[0], -1)
            embed_A = self.C[hop](story.contiguous().view(story_size[0], -1))#.long()) # b * (m * s) * e
            embed_A = embed_A.view(story_size+(embed_A.size(-1),)) # b * m * s * e
            embed_A = torch.sum(embed_A, 2).squeeze(2) # b * m * e
            if not args["ablationH"]: #消除实验，如果不加入隐含状态也就是Global Encoder的Context RNN没有隐含状态没有写入EK
                embed_A = self.add_lm_embedding(embed_A, kb_len, conv_len, dh_outputs)
            embed_A = self.dropout_layer(embed_A)  #我没有添加droptout
            
            # if(len(list(u[-1].size()))==1):
            #     u[-1] = u[-1].unsqueeze(0) ## used for bsz = 1.
            # u_temp = u[-1].unsqueeze(1).expand_as(embed_A)
            u_temp = query.unsqueeze(1).expand_as(embed_A)
            prob_logit = torch.sum(embed_A*u_temp, 2)  # 计算EK步骤的attention weight,也就是全局指针
            prob_   = self.softmax(prob_logit)
            
            embed_C = self.C[hop+1](story.contiguous().view(story_size[0], -1)) #词袋表示，去除第k hop
            embed_C = embed_C.view(story_size+(embed_C.size(-1),)) 
            embed_C = torch.sum(embed_C, 2).squeeze(2)
            if not args["ablationH"]:
            #消除实验是将Context RNN的输出而不是最后的隐含状态写入KB，论文指的隐状态就是输出，因为RNN输出是对文本转换，将转化后的文本存入
            #KB中符合常识
                embed_C = self.add_lm_embedding(embed_C, kb_len, conv_len, dh_outputs)

            prob = prob_.unsqueeze(2).expand_as(embed_C)
            o_k  = torch.sum(embed_C*prob, 1) #将memory中的词袋表示乘以注意力，得到从memory中读取出来的内容
            # u_k = u[-1] + o_k  #u是Context RNN的最后隐含态，作为attention的query向量，根据论文中的公司更新
            # u.append(u_k)
            query = query + o_k
            self.m_story.append(embed_A)
        self.m_story.append(embed_C)
        return self.sigmoid(prob_logit), query # u[-1] #global pointer，和KB中读出来的值

    def forward(self, query_vector, global_pointer):  #local encoder要queryEK得到local pointer
        # u = [query_vector]
        for hop in range(self.max_hops):
            m_A = self.m_story[hop] 
            if not args["ablationG"]:
                m_A = m_A * global_pointer.unsqueeze(2).expand_as(m_A) 
            # if(len(list(u[-1].size()))==1):
            #     u[-1] = u[-1].unsqueeze(0) ## used for bsz = 1.
            #u_temp = u[-1].unsqueeze(1).expand_as(m_A)
            u_temp = query_vector.unsqueeze(1).expand_as(m_A)
            prob_logits = torch.sum(m_A*u_temp, 2)
            prob_soft   = self.softmax(prob_logits)
            m_C = self.m_story[hop+1] 
            if not args["ablationG"]: #global指针的消除实验
                m_C = m_C * global_pointer.unsqueeze(2).expand_as(m_C)
            prob = prob_soft.unsqueeze(2).expand_as(m_C)
            o_k  = torch.sum(m_C*prob, 1)
            # u_k = u[-1] + o_k
            # u.append(u_k)
            query_vector = query_vector + o_k
        return prob_soft, prob_logits


class LocalMemoryDecoder(nn.Module):
    def __init__(self, shared_emb, lang, embedding_dim, hop, dropout):
        super(LocalMemoryDecoder, self).__init__()
        self.num_vocab = lang.n_words
        self.lang = lang
        self.max_hops = hop
        self.embedding_dim = embedding_dim
        self.dropout = dropout
        self.dropout_layer = nn.Dropout(dropout) 
        self.C = shared_emb   # [116 128]
        self.softmax = nn.Softmax(dim=1)
        self.sketch_rnn = nn.GRU(embedding_dim, embedding_dim, dropout=dropout)
        self.relu = nn.ReLU()
        self.projector = nn.Linear(2*embedding_dim, embedding_dim)
        #self.conv_layer = nn.Conv1d(embedding_dim, embedding_dim, 5, padding=2)
        self.softmax = nn.Softmax(dim = 1)

    def forward(self, extKnow, story_size, story_lengths, copy_list, encode_hidden, target_batches, max_target_length, batch_size, use_teacher_forcing, get_decoded_words, global_pointer):
        # Initialize variables for vocab and pointer
        all_decoder_outputs_vocab = _cuda(torch.zeros(max_target_length, batch_size, self.num_vocab))
        all_decoder_outputs_ptr = _cuda(torch.zeros(max_target_length, batch_size, story_size[1]))
        decoder_input = _cuda(torch.LongTensor([SOS_token] * batch_size))
        memory_mask_for_step = _cuda(torch.ones(story_size[0], story_size[1]))  # [8 70]
        decoded_fine, decoded_coarse = [], []
        
        hidden = self.relu(self.projector(encode_hidden)).unsqueeze(0) #the encoded dialogue history he
        
        # Start to generate word-by-word
        for t in range(max_target_length):
            embed_q = self.dropout_layer(self.C(decoder_input)) # b * e
            if len(embed_q.size()) == 1: embed_q = embed_q.unsqueeze(0)
            _, hidden = self.sketch_rnn(embed_q.unsqueeze(0), hidden)  # [1 8 128]
            query_vector = hidden[0] #sketch_rnn的最后隐含态作为query EK还有预测下一个词  [8 128]
            
            p_vocab = self.attend_vocab(self.C.weight, hidden.squeeze(0))  # [8 116]
            all_decoder_outputs_vocab[t] = p_vocab
            _, topvi = p_vocab.data.topk(1)  # [8 1]
            
            # query the external konwledge using the hidden state of sketch RNN
            prob_soft, prob_logits = extKnow(query_vector, global_pointer)  #这里使用了EK的forward来  [8 70]
            all_decoder_outputs_ptr[t] = prob_logits  # local pointer

            #下面这些暂时看不懂

            if use_teacher_forcing:   # 需要添加这个，效果才会提升较多
                decoder_input = target_batches[:, t]  # max_target_length要是每个批次的最长长度，由于use_teacher_forcing有随机性，所以这个不容易出发，是一个随机的bug
            else:
                decoder_input = topvi.squeeze()
            
            if get_decoded_words:

                search_len = min(5, min(story_lengths))
                prob_soft = prob_soft * memory_mask_for_step
                _, toppi = prob_soft.data.topk(search_len)  # [8 search_len]
                temp_f, temp_c = [], []
                
                for bi in range(batch_size):
                    token = topvi[bi].item() #topvi[:,0][bi].item()
                    temp_c.append(self.lang.index2word[token])
                    
                    if '@' in self.lang.index2word[token]:
                        cw = 'UNK'
                        for i in range(search_len):
                            # ti = toppi[:,i][bi]
                            if toppi[bi][i] < story_lengths[bi]-1:
                                cw = copy_list[bi][toppi[bi][i].item()]
                                break
                        temp_f.append(cw)
                        
                        if args['record']:
                            memory_mask_for_step[bi, toppi[bi][i].item()] = 0
                    else:
                        temp_f.append(self.lang.index2word[token])

                decoded_fine.append(temp_f)
                decoded_coarse.append(temp_c)

        return all_decoder_outputs_vocab, all_decoder_outputs_ptr, decoded_fine, decoded_coarse

    def attend_vocab(self, seq, cond):
        scores_ = cond.matmul(seq.transpose(1,0))
        scores = F.softmax(scores_, dim=1)
        return scores_



class AttrProxy(object):
    """
    Translates index lookups into attribute lookups.
    To implement some trick which able to use list of nn.Module in a nn.Module
    see https://discuss.pytorch.org/t/list-of-nn-module-in-a-nn-module/219/2
    """
    def __init__(self, module, prefix):
        self.module = module
        self.prefix = prefix

    def __getitem__(self, i):
        return getattr(self.module, self.prefix + str(i))
