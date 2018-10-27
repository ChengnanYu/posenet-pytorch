import torch
import torch.nn as nn
from torch.nn import init
from torch.nn import functional as F
import functools
from torch.autograd import Variable
from torch.optim import lr_scheduler
import numpy as np
###############################################################################
# Functions
###############################################################################


def weight_init_googlenet(key, module, weights=None):
    if weights is None:
        init.constant_(module.bias.data, 0.0)
        if key == "XYZ":
            init.normal_(module.weight.data, 0.0, 0.5)
        else:
            init.normal_(module.weight.data, 0.0, 0.01)
    else:
        # print(key, weights[(key+"_1").encode()].shape, module.bias.size())
        module.bias.data[...] = torch.from_numpy(weights[(key+"_1").encode()])
        module.weight.data[...] = torch.from_numpy(weights[(key+"_0").encode()])
    return module

def get_scheduler(optimizer, opt):
    if opt.lr_policy == 'lambda':
        def lambda_rule(epoch):
            lr_l = 1.0 - max(0, epoch + 1 + opt.epoch_count - opt.niter) / float(opt.niter_decay + 1)
            return lr_l
        scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lambda_rule)
    elif opt.lr_policy == 'step':
        scheduler = lr_scheduler.StepLR(optimizer, step_size=opt.lr_decay_iters, gamma=0.1)
    elif opt.lr_policy == 'plateau':
        scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.2, threshold=0.01, patience=5)
    else:
        return NotImplementedError('learning rate policy [%s] is not implemented', opt.lr_policy)
    return scheduler


def define_G(input_nc, output_nc, ngf, which_model_netG, sx, sq, sequence_length, norm='batch', use_dropout=False, init_type='normal', gpu_ids=[],
             init_from=None, isTest=False):
    netG = None
    use_gpu = len(gpu_ids) > 0

    if use_gpu:
        assert(torch.cuda.is_available())

    if which_model_netG == 'posenet':
        netG = PoseNet(input_nc, sx, sq, sequence_length, weights=init_from, isTest=isTest, gpu_ids=gpu_ids)
    else:
        raise NotImplementedError('Generator model name [%s] is not recognized' % which_model_netG)
    if len(gpu_ids) > 0:
        netG.cuda(gpu_ids[0])
    return netG

##############################################################################
# Classes
##############################################################################

# defines the spatial LSTM after googlenet
'''
class SpLSTM(nn.Module):
    def __init__(self, weights=None):
        super(SpLSTM, self).__init__()
        self.lstm1 = nn.LSTM(input_size=64, hidden_size=32, num_layers=1, batch_first=True)
        self.lstm2 = nn.LSTM(input_size=32, hidden_size=32, num_layers=1, batch_first=True)

    def forward(self, input):
        #input shape [batch size, 2048]
        input1 = input.view(input.size(0),32,64)
        input2 = input1.transpose(1,2)
        #inverse the input tensor at axis=1
        inv_idx = torch.arange(input.size(1)-1, -1, -1, dtype=torch.long, device=input.device)
        inv_input = input.index_select(1,inv_idx)
        input3 = inv_input.view(input.size(0),32,64)
        input4 = input3.transpose(1,2)
        outlstm1, _ = self.lstm1(input1) #shape (batch_size, seq_length, hidden_size)
        outlstm2, _ = self.lstm2(input2)
        outlstm3, _ = self.lstm1(input3)
        outlstm4, _ = self.lstm2(input4)
        out1 = outlstm1[:, -1, :] # take the state of the last time step
        out2 = outlstm2[:, -1, :]
        out3 = outlstm3[:, -1, :]
        out4 = outlstm4[:, -1, :]
        output = torch.cat((out1, out2, out3, out4), 1) #shape [batch size, 128]
        return output
'''

class SpLSTM(nn.Module):
    def __init__(self, seq_len1, seq_len2, hidden_size, weights=None):
        super(SpLSTM, self).__init__()
        self.seq_len1 = seq_len1
        self.seq_len2 = seq_len2
        self.hidden_size = hidden_size
        self.lstm1 = nn.LSTM(input_size=self.seq_len1, hidden_size=self.hidden_size, num_layers=1, batch_first=True, bidirectional=True)
        self.lstm2 = nn.LSTM(input_size=self.seq_len2, hidden_size=self.hidden_size, num_layers=1, batch_first=True, bidirectional=True)

    def forward(self, input):
        #input shape [batch size, 2048]
        input1 = input.view(input.size(0), self.seq_len2, self.seq_len1)
        input2 = input1.transpose(1,2)
        # initialize hidden states and cell states of shape (num_layers * num_directions, batch, hidden_size)
        hidden1 = (torch.randn(2, input.size(0), self.hidden_size).to(input.device),torch.randn(2, input.size(0), self.hidden_size).to(input.device))
        hidden2 = (torch.randn(2, input.size(0), self.hidden_size).to(input.device),torch.randn(2, input.size(0), self.hidden_size).to(input.device))
        outlstm1, hidden1 = self.lstm1(input1, hidden1) #shape (batch_size, seq_length, hidden_size*2)
        outlstm2, hidden2 = self.lstm2(input2, hidden2)
        out1 = hidden1[0][0]
        out2 = hidden1[0][1]
        out3 = hidden2[0][0]
        out4 = hidden2[0][1]
        output = torch.cat((out1, out2, out3, out4), 1) #shape [batch size, 4*hiddensize]
        return output

# defines the sequence LSTM after spatial LSTM
class SqLSTM(nn.Module):
    def __init__(self, sequence_length, weights=None):
        super(SqLSTM, self).__init__()
        self.sequence_length = sequence_length
        #self.lstm = nn.LSTM(input_size=5120, hidden_size=1024, num_layers=2, batch_first=True, bidirectional=True)
        self.lstm = nn.LSTM(input_size=5120, hidden_size=1024, num_layers=2, batch_first=True, bidirectional=True)
        #self.cls_splstm = SpLSTM(seq_len1=64, seq_len2=32, hidden_size=256)
        #self.cls_fc_xy = weight_init_googlenet("XYZ", nn.Linear(256*4, 3))
        self.cls_fc_xy = weight_init_googlenet("XYZ", nn.Linear(1024*2, 3))
        #self.cls_fc_wpqr = weight_init_googlenet("WPQR", nn.Linear(256*4, 4))
        self.cls_fc_wpqr = weight_init_googlenet("WPQR", nn.Linear(5120, 4))

    def forward(self, input):
        #input shape [BATCH SIZE, 4096], reshape into [batch size, sequence length, 4096]
        input = input.view(-1,self.sequence_length,input.size(1))
        outlstm, _ = self.lstm(input) #shape (batch size, sequence length, hidden size * 2)
        output_xy = self.cls_fc_xy(outlstm) #shape (batch size, sequence length, 3)
        output_wpqr = self.cls_fc_wpqr(input) #shape (batch size, sequence length, 4)
        output_wpqr = F.normalize(output_wpqr, p=2, dim=2)
        return [output_xy, output_wpqr]

class SqCNN(nn.Module):
    def __init__(self, sequence_length, weights=None):
        super(SqCNN, self).__init__()
        self.sequence_length = sequence_length
        self.temCNN = nn.Sequential(*[
            nn.Conv2d(self.sequence_length, 64, kernel_size=1, padding=0),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(kernel_size=2, stride=2),
            nn.Conv2d(64, 128, kernel_size=1, padding=0),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(kernel_size=2, stride=2),
            nn.Conv2d(128, 256, kernel_size=1, padding=0),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(kernel_size=2, stride=2),
            #nn.Conv2d(256, 512, kernel_size=3, padding=1),
            #nn.BatchNorm2d(512),
            #nn.ReLU(inplace=True),
            #nn.MaxPool2d(kernel_size=2, stride=2),
            nn.AvgPool2d(4)])
        #self.lstm = nn.LSTM(input_size=5120, hidden_size=1024, num_layers=2, batch_first=True, bidirectional=True)
        #self.lstm = nn.LSTM(input_size=4096, hidden_size=1024, num_layers=2, batch_first=True, bidirectional=True)
        #self.cls_splstm = SpLSTM(seq_len1=64, seq_len2=32, hidden_size=256)
        #self.cls_fc_xy = weight_init_googlenet("XYZ", nn.Linear(256*4, 3))
        self.cls_fc_xy = weight_init_googlenet("XYZ", nn.Linear(256, 3*self.sequence_length))
        #self.cls_fc_wpqr = weight_init_googlenet("WPQR", nn.Linear(256*4, 4))
        self.cls_fc_wpqr = weight_init_googlenet("WPQR", nn.Linear(256, 4*self.sequence_length))

    def forward(self, input):
        #input shape [BATCH SIZE, 4096], reshape into [batch size, sequence length, 4096]
        input = input.view(-1,self.sequence_length,input.size(1))
        input = input.view(input.size(0),input.size(1),32,32)
        out_temCNN = self.temCNN(input)
        out_temCNN = out_temCNN.view(out_temCNN.size(0),out_temCNN.size(1))
        #outlstm, _ = self.lstm(input) #shape (batch size, sequence length, hidden size * 2)
        #outsplstm = torch.zeros([outlstm.size(0),outlstm.size(1),256*4],device=outlstm.device)
        #for i in range(self.sequence_length):
        #    outsplstm[:,i,:] = self.cls_splstm(outlstm[:,i,:])
        output_xy = self.cls_fc_xy(out_temCNN)
        output_xy = output_xy.view(output_xy.size(0),self.sequence_length,3) #shape (batch size, sequence length, 3)
        output_wpqr = self.cls_fc_wpqr(out_temCNN)
        output_wpqr = output_wpqr.view(output_wpqr.size(0),self.sequence_length,4) #shape (batch size, sequence length, 4)
        output_wpqr = F.normalize(output_wpqr, p=2, dim=2)
        return [output_xy, output_wpqr]

# defines the regression heads for googlenet
class SpLSTMHead1(nn.Module):
    def __init__(self, lossID, weights=None):
        super(SpLSTMHead1, self).__init__()
        nc = {"loss1": 512, "loss2": 528}
        #self.projection = nn.Sequential(*[nn.AvgPool2d(kernel_size=5, stride=3),
        #                                      weight_init_googlenet(lossID+"/conv", nn.Conv2d(nc[lossID], 128, kernel_size=1), weights),
        #                                      nn.ReLU(inplace=True), nn.Dropout(0.7)])
        self.projection = nn.Sequential(*[nn.AvgPool2d(kernel_size=5, stride=3),
                                              weight_init_googlenet(lossID+"/conv", nn.Conv2d(nc[lossID], 128, kernel_size=1), weights),
                                              nn.ReLU(inplace=True)])
        #self.projection = nn.Sequential(*[nn.AvgPool2d(kernel_size=5, stride=3),
        #                                      nn.Conv2d(nc[lossID], 64, kernel_size=1),
        #                                      nn.ReLU(inplace=True)])
        #self.cls_splstm = SpLSTM(seq_len1=32, seq_len2=32, hidden_size=256)
        #self.cls_fc_pose = nn.Sequential(*[weight_init_googlenet(lossID+"/fc", nn.Linear(2048, 1024), weights),
        #                                       nn.ReLU(inplace=True), self.cls_splstm,
        #                                       nn.Dropout(0.7)])
        #self.cls_fc_pose = nn.Sequential(*[weight_init_googlenet(lossID+"/fc", nn.Linear(2048, 1024), weights),
        #                                       nn.ReLU(inplace=True),
        #                                       nn.Dropout(0.7)])
        #self.cls_sqcnn = SqCNN(self.sequence_length)

    def forward(self, input):
        output = self.projection(input)
        #output = self.cls_fc_pose(output.view(output.size(0), -1))
        output = output.view(output.size(0), -1)
        #output = self.cls_sqcnn(output)
        return output

class SpLSTMHead2(nn.Module):
    def __init__(self, lossID, weights=None):
        super(SpLSTMHead2, self).__init__()
        #self.projection = nn.Sequential(*[nn.AvgPool2d(kernel_size=7, stride=1), nn.Dropout(0.5)])
        self.projection = nn.AvgPool2d(kernel_size=7, stride=1)
        #self.cls_splstm = SpLSTM(seq_len1=64, seq_len2=32, hidden_size=256)
        #self.cls_fc_pose = nn.Sequential(*[weight_init_googlenet("pose", nn.Linear(1024, 2048)),
        #                                       nn.ReLU(inplace=True), self.cls_splstm, 
        #                                       nn.Dropout(0.5)])
        #self.cls_fc_pose = nn.Sequential(*[weight_init_googlenet("pose", nn.Linear(1024, 2048)),
        #                                       nn.ReLU(inplace=True),
        #                                       nn.Dropout(0.5)])
        #self.cls_sqcnn = SqCNN(self.sequence_length)

    def forward(self, input):
        output = self.projection(input)
        #output = self.cls_fc_pose(output.view(output.size(0), -1))
        output = output.view(output.size(0), -1)
        #output = self.cls_sqcnn(output)
        return output

# define inception block for GoogleNet
class InceptionBlock(nn.Module):
    def __init__(self, incp, input_nc, x1_nc, x3_reduce_nc, x3_nc, x5_reduce_nc,
                 x5_nc, proj_nc, weights=None, gpu_ids=[]):
        super(InceptionBlock, self).__init__()
        self.gpu_ids = gpu_ids
        # first
        self.branch_x1 = nn.Sequential(*[
            weight_init_googlenet("inception_"+incp+"/1x1", nn.Conv2d(input_nc, x1_nc, kernel_size=1), weights),
            nn.ReLU(inplace=True)])

        self.branch_x3 = nn.Sequential(*[
            weight_init_googlenet("inception_"+incp+"/3x3_reduce", nn.Conv2d(input_nc, x3_reduce_nc, kernel_size=1), weights),
            nn.ReLU(inplace=True),
            weight_init_googlenet("inception_"+incp+"/3x3", nn.Conv2d(x3_reduce_nc, x3_nc, kernel_size=3, padding=1), weights),
            nn.ReLU(inplace=True)])

        self.branch_x5 = nn.Sequential(*[
            weight_init_googlenet("inception_"+incp+"/5x5_reduce", nn.Conv2d(input_nc, x5_reduce_nc, kernel_size=1), weights),
            nn.ReLU(inplace=True),
            weight_init_googlenet("inception_"+incp+"/5x5", nn.Conv2d(x5_reduce_nc, x5_nc, kernel_size=5, padding=2), weights),
            nn.ReLU(inplace=True)])

        self.branch_proj = nn.Sequential(*[
            nn.MaxPool2d(kernel_size=3, stride=1, padding=1),
            weight_init_googlenet("inception_"+incp+"/pool_proj", nn.Conv2d(input_nc, proj_nc, kernel_size=1), weights),
            nn.ReLU(inplace=True)])

        if incp in ["3b", "4e"]:
            self.pool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        else:
            self.pool = None

    def forward(self, input):
        outputs = [self.branch_x1(input), self.branch_x3(input),
                   self.branch_x5(input), self.branch_proj(input)]
        # print([[o.size()] for o in outputs])
        output = torch.cat(outputs, 1)
        if self.pool is not None:
            return self.pool(output)
        return output

class PoseNet(nn.Module):
    def __init__(self, input_nc, sx, sq, sequence_length, weights=None, isTest=False, gpu_ids=[]):
        super(PoseNet, self).__init__()
        self.gpu_ids = gpu_ids
        self.isTest = isTest
        self.sequence_length = sequence_length
        self.before_inception = nn.Sequential(*[
            weight_init_googlenet("conv1/7x7_s2", nn.Conv2d(input_nc, 64, kernel_size=7, stride=2, padding=3), weights),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            nn.LocalResponseNorm(5),
            weight_init_googlenet("conv2/3x3_reduce", nn.Conv2d(64, 64, kernel_size=1), weights),
            nn.ReLU(inplace=True),
            weight_init_googlenet("conv2/3x3", nn.Conv2d(64, 192, kernel_size=3, padding=1), weights),
            nn.ReLU(inplace=True),
            nn.LocalResponseNorm(5),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
            ])

        self.inception_3a = InceptionBlock("3a", 192, 64, 96, 128, 16, 32, 32, weights, gpu_ids)
        self.inception_3b = InceptionBlock("3b", 256, 128, 128, 192, 32, 96, 64, weights, gpu_ids)
        self.inception_4a = InceptionBlock("4a", 480, 192, 96, 208, 16, 48, 64, weights, gpu_ids)
        self.inception_4b = InceptionBlock("4b", 512, 160, 112, 224, 24, 64, 64, weights, gpu_ids)
        self.inception_4c = InceptionBlock("4c", 512, 128, 128, 256, 24, 64, 64, weights, gpu_ids)
        self.inception_4d = InceptionBlock("4d", 512, 112, 144, 288, 32, 64, 64, weights, gpu_ids)
        self.inception_4e = InceptionBlock("4e", 528, 256, 160, 320, 32, 128, 128, weights, gpu_ids)
        self.inception_5a = InceptionBlock("5a", 832, 256, 160, 320, 32, 128, 128, weights, gpu_ids)
        self.inception_5b = InceptionBlock("5b", 832, 384, 192, 384, 48, 128, 128, weights, gpu_ids)

        self.cls1_splstm = SpLSTMHead1(lossID="loss1", weights=weights)
        self.cls2_splstm = SpLSTMHead1(lossID="loss2", weights=weights)
        self.cls3_splstm = SpLSTMHead2(lossID="loss3", weights=weights)
        
        self.cls_sqlstm = SqLSTM(self.sequence_length)
        #self.cls_sqcnn = SqCNN(self.sequence_length)

        self.model = nn.Sequential(*[self.inception_3a, self.inception_3b,
                                   self.inception_4a, self.inception_4b,
                                   self.inception_4c, self.inception_4d,
                                   self.inception_4e, self.inception_5a,
                                   self.inception_5b, self.cls1_splstm,
                                   self.cls2_splstm, self.cls3_splstm, self.cls_sqlstm
                                   ])
        if self.isTest:
            self.model.eval() # ensure Dropout is deactivated during test

    def forward(self, input):

        output_bf = self.before_inception(input)
        output_3a = self.inception_3a(output_bf)
        output_3b = self.inception_3b(output_3a)
        output_4a = self.inception_4a(output_3b)
        output_4b = self.inception_4b(output_4a)
        output_4c = self.inception_4c(output_4b)
        output_4d = self.inception_4d(output_4c)
        output_4e = self.inception_4e(output_4d)
        output_5a = self.inception_5a(output_4e)
        output_5b = self.inception_5b(output_5a)
        output_cls1 = self.cls1_splstm(output_4a)
        output_cls2 = self.cls2_splstm(output_4d)
        output_cls3 = self.cls3_splstm(output_5b)
        output_SpLSTMHeads = torch.cat((output_cls1, output_cls2, output_cls3), 1)
        output_sqlstm = self.cls_sqlstm(output_SpLSTMHeads)
        #output_sqcnn = self.cls_sqcnn(output_SpLSTMHeads)

        return output_sqlstm
        #if not self.isTest:
        #    return output_cls1 + output_cls2 +  output_cls3
        #return output_cls3
