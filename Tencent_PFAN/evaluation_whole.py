# -----------------------------------------------------------
# Position Focused Attention Network (PFAN) implementation based on 
# another network Stacked Cross Attention Network (https://arxiv.org/abs/1803.08024)
# the code of SCAN: https://github.com/kuanghuei/SCAN
# ---------------------------------------------------------------
"""Evaluation"""

from __future__ import print_function
import os

import sys
from data_whole import get_test_loader
import time
import numpy as np
from vocab import Vocabulary, deserialize_vocab  # NOQA
import torch
from model_whole import SCAN, xattn_score_t2i, xattn_score_i2t
from collections import OrderedDict
import time
from torch.autograd import Variable

class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=0):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / (.0001 + self.count)

    def __str__(self):
        """String representation for logging
        """
        # for values that should be recorded exactly e.g. iteration number
        if self.count == 0:
            return str(self.val)
        # for stats
        return '%.4f (%.4f)' % (self.val, self.avg)


class LogCollector(object):
    """A collection of logging objects that can change from train to val"""

    def __init__(self):
        # to keep the order of logged variables deterministic
        self.meters = OrderedDict()

    def update(self, k, v, n=0):
        # create a new meter if previously not recorded
        if k not in self.meters:
            self.meters[k] = AverageMeter()
        self.meters[k].update(v, n)

    def __str__(self):
        """Concatenate the meters in one log line
        """
        s = ''
        for i, (k, v) in enumerate(self.meters.iteritems()):
            if i > 0:
                s += '  '
            s += k + ' ' + str(v)
        return s

    def tb_log(self, tb_logger, prefix='', step=None):
        """Log using tensorboard
        """
        for k, v in self.meters.iteritems():
            tb_logger.log_value(prefix + k, v.val, step=step)


def encode_data(model, data_loader, log_step=10, logging=print):
    """Encode all images and captions loadable by `data_loader`
    """
    batch_time = AverageMeter()
    val_logger = LogCollector()

    # switch to evaluate mode
    model.val_start()

    end = time.time()

    # np array to keep all the embeddings
    img_embs = None
    whole_img_embs = None
    cap_embs = None
    cap_lens = None
    final_cap_embs = None

    max_n_word = 0
    for i, (images, whole_images, boxes, captions, lengths, ids) in enumerate(data_loader):
        max_n_word = max(max_n_word, max(lengths))

    for i, (images, whole_images, boxes, captions, lengths, ids) in enumerate(data_loader):
        # make sure val logger is used
        model.logger = val_logger

        # compute the embeddings
        img_emb, whole_img_emb, cap_emb, cap_len, final_cap_emb = model.forward_emb(images, whole_images, boxes, captions, lengths, volatile=True)
        #print(img_emb)
        if img_embs is None:
            if img_emb.dim() == 3:
                img_embs = np.zeros((len(data_loader.dataset), img_emb.size(1), img_emb.size(2)))
            else:
                img_embs = np.zeros((len(data_loader.dataset), img_emb.size(1)))
            whole_img_embs = np.zeros((len(data_loader.dataset), whole_img_emb.size(1)))
            cap_embs = np.zeros((len(data_loader.dataset), max_n_word, cap_emb.size(2)))
            cap_lens = [0] * len(data_loader.dataset)
            final_cap_embs = np.zeros((len(data_loader.dataset), final_cap_emb.size(1)))

        # cache embeddings
        img_embs[ids,:,:] = img_emb.data.cpu().numpy().copy()
        whole_img_embs[ids,:] = whole_img_emb.data.cpu().numpy().copy()
        cap_embs[ids,:max(lengths),:] = cap_emb.data.cpu().numpy().copy()
        final_cap_embs[ids,:] = final_cap_emb.data.cpu().numpy().copy()
        for j, nid in enumerate(ids):
            cap_lens[nid] = cap_len[j]

        # measure accuracy and record loss
        model.forward_loss(img_emb, whole_img_emb, cap_emb, cap_len, final_cap_emb)

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % log_step == 0:
            logging('Test: [{0}/{1}]\t'
                    '{e_log}\t'
                    'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                    .format(
                        i, len(data_loader), batch_time=batch_time,
                        e_log=str(model.logger)))
        del images, whole_images, boxes, captions
    return img_embs, whole_img_embs, cap_embs, cap_lens, final_cap_embs


def evalrank(model_path, data_path=None, split='dev', fold5=False):
    """
    Evaluate a trained model on either dev or test. If `fold5=True`, 5 fold
    cross-validation is done (only for MSCOCO). Otherwise, the full data is
    used for evaluation.
    """
    # load model and options
    checkpoint = torch.load(model_path)
    opt = checkpoint['opt']
    print(opt)
    if data_path is not None:
        opt.data_path = data_path

    # load vocabulary used by the model
    vocab = deserialize_vocab(os.path.join(opt.vocab_path, '%s_vocab.json' % opt.data_name))
    opt.vocab_size = len(vocab)

    # construct model
    model = SCAN(opt)

    # load model state
    model.load_state_dict(checkpoint['model'])

    print('Loading dataset')
    data_loader = get_test_loader(split, opt.data_name, vocab,
                                  opt.batch_size, opt.workers, opt)

    print('Computing results...')
    img_embs, cap_embs, cap_lens, final_cap_embs = encode_data(model, data_loader)
    print('Images: %d, Captions: %d' %
          (img_embs.shape[0], cap_embs.shape[0]))

    print("Fold5 flag:", fold5)
    if not fold5:
        # no cross-validation, full evaluation
        #img_embs = np.array([img_embs[i] for i in range(0, len(img_embs), 5)])
        cap_embs = np.array([cap_embs[0]]) # one text to multiply images
        print("Shape in img_embs", img_embs.shape)
        #print("Shape in whole_img_embs", whole_img_embs.shape)

        start = time.time()
        if opt.cross_attn == 't2i':
            sims = shard_xattn_t2i(img_embs, cap_embs, cap_lens, final_cap_embs, opt, shard_size=128)
        elif opt.cross_attn == 'i2t':
            sims = shard_xattn_i2t(img_embs, cap_embs, cap_lens, opt, shard_size=128)
        else:
            raise NotImplementedError
        end = time.time()
        print("calculate similarity time:", end-start)
        print("Sims shape:", sims.shape)
        np.save("Final_sims_wb.npy", sims)
        r, rt = i2t(img_embs, cap_embs, sims, return_ranks=True)
        ri, rti = t2i(img_embs, cap_embs, sims, return_ranks=True)
        ar = (r[0] + r[1] + r[2]) / 3
        ari = (ri[0] + ri[1] + ri[2]) / 3
        rsum = r[0] + r[1] + r[2] + ri[0] + ri[1] + ri[2]
        print("rsum: %.1f" % rsum)
        print("Average i2t Recall: %.1f" % ar)
        print("Image to text: %.1f %.1f %.1f %.1f %.1f" % r)
        print("Average t2i Recall: %.1f" % ari)
        print("Text to image: %.1f %.1f %.1f %.1f %.1f" % ri)
    else:
        # 5fold cross-validation, only for MSCOCO
        results = []
        for i in range(5):
            img_embs_shard = img_embs[i * 5000:(i + 1) * 5000:5]
            cap_embs_shard = cap_embs[i * 5000:(i + 1) * 5000]
            cap_lens_shard = cap_lens[i * 5000:(i + 1) * 5000]
            start = time.time()
            if opt.cross_attn == 't2i':
                sims = shard_xattn_t2i(img_embs_shard, cap_embs_shard, cap_lens_shard, opt, shard_size=128)
            elif opt.cross_attn == 'i2t':
                sims = shard_xattn_i2t(img_embs_shard, cap_embs_shard, cap_lens_shard, opt, shard_size=128)
            else:
                raise NotImplementedError
            end = time.time()
            print("calculate similarity time:", end-start)
            print("Sims shape:", sims.shape)
            r, rt0 = i2t(img_embs_shard, cap_embs_shard, sims, return_ranks=True)
            print("Image to text: %.1f, %.1f, %.1f, %.1f, %.1f" % r)
            ri, rti0 = t2i(img_embs_shard, cap_embs_shard, sims, return_ranks=True)
            print("Text to image: %.1f, %.1f, %.1f, %.1f, %.1f" % ri)

            if i == 0:
                rt, rti = rt0, rti0
            ar = (r[0] + r[1] + r[2]) / 3
            ari = (ri[0] + ri[1] + ri[2]) / 3
            rsum = r[0] + r[1] + r[2] + ri[0] + ri[1] + ri[2]
            print("rsum: %.1f ar: %.1f ari: %.1f" % (rsum, ar, ari))
            results += [list(r) + list(ri) + [ar, ari, rsum]]

        print("-----------------------------------")
        print("Mean metrics: ")
        mean_metrics = tuple(np.array(results).mean(axis=0).flatten())
        print("rsum: %.1f" % (mean_metrics[10] * 6))
        print("Average i2t Recall: %.1f" % mean_metrics[11])
        print("Image to text: %.1f %.1f %.1f %.1f %.1f" %
              mean_metrics[:5])
        print("Average t2i Recall: %.1f" % mean_metrics[12])
        print("Text to image: %.1f %.1f %.1f %.1f %.1f" %
              mean_metrics[5:10])

    torch.save({'rt': rt, 'rti': rti}, 'ranks.pth.tar')


def softmax(X, axis):
    """
    Compute the softmax of each element along an axis of X.
    """
    y = np.atleast_2d(X)
    # subtract the max for numerical stability
    y = y - np.expand_dims(np.max(y, axis = axis), axis)
    # exponentiate y
    y = np.exp(y)
    # take the sum along the specified axis
    ax_sum = np.expand_dims(np.sum(y, axis = axis), axis)
    # finally: divide elementwise
    p = y / ax_sum
    return p


def shard_xattn_t2i(images, whole_images, captions, caplens, final_captions, opt, shard_size=128):
    """
    Computer pairwise t2i image-caption distance with locality sharding
    """
    n_im_shard = (len(images)-1)/shard_size + 1
    n_cap_shard = (len(captions)-1)/shard_size + 1

    d = np.zeros((len(images), len(captions)))
    for i in range(n_im_shard):
        im_start, im_end = shard_size*i, min(shard_size*(i+1), len(images))
        for j in range(n_cap_shard):
            sys.stdout.write('\r>> shard_xattn_t2i batch (%d,%d)' % (i,j))
            cap_start, cap_end = shard_size*j, min(shard_size*(j+1), len(captions))
            im = Variable(torch.from_numpy(images[im_start:im_end]), volatile=True)
            w_im = Variable(torch.from_numpy(whole_images[im_start: im_end]), volatile=True)
            s = Variable(torch.from_numpy(captions[cap_start:cap_end]), volatile=True)
            l = caplens[cap_start:cap_end]
            f_s = Variable(torch.from_numpy(final_captions[cap_start: cap_end]), volatile=True)
            if torch.cuda.is_available():
                im = im.cuda()
                w_im = w_im.cuda()
                s = s.cuda()
                f_s = f_s.cuda()
            #print("Captioins", captions)
            #print("Final captions", final_captions)
            sim = xattn_score_t2i(im, w_im, s, l, f_s, opt)
            d[im_start:im_end, cap_start:cap_end] = sim.data.cpu().numpy()
    sys.stdout.write('\n')
    return d


def shard_xattn_i2t(images, captions, caplens, opt, shard_size=128):
    """
    Computer pairwise i2t image-caption distance with locality sharding
    """
    n_im_shard = (len(images)-1)/shard_size + 1
    n_cap_shard = (len(captions)-1)/shard_size + 1

    d = np.zeros((len(images), len(captions)))
    for i in range(n_im_shard):
        im_start, im_end = shard_size*i, min(shard_size*(i+1), len(images))
        for j in range(n_cap_shard):
            sys.stdout.write('\r>> shard_xattn_i2t batch (%d,%d)' % (i,j))
            cap_start, cap_end = shard_size*j, min(shard_size*(j+1), len(captions))
            im = Variable(torch.from_numpy(images[im_start:im_end]), volatile=True)
            #w_im = Variable(torch.from_numpy(whole_images[im_start: im_end]), volatile=True).cuda()
            s = Variable(torch.from_numpy(captions[cap_start:cap_end]), volatile=True)
            if torch.cuda.is_available():
                im = im.cuda()
                s = s.cuda()
            l = caplens[cap_start:cap_end]
            sim = xattn_score_i2t(im, s, l, opt)
            d[im_start:im_end, cap_start:cap_end] = sim.data.cpu().numpy()
    sys.stdout.write('\n')
    return d


def i2t(images, captions, sims, npts=None, return_ranks=False):
    """
    Images->Text (Image Annotation)
    Images: (N, n_region, d) matrix of images
    Captions: (5N, max_n_word, d) matrix of captions
    CapLens: (5N) array of caption lengths
    sims: (N, 5N) matrix of similarity im-cap
    """
    print("Sims shape", sims.shape)
    npts = images.shape[0]
    ranks = np.zeros(npts)
    top1 = np.zeros(npts)
    #fw = open('not_good_i2t.txt','w')
    #fw.write('Image\tText\tRank\tTop1\tSim\tSim-top1\n')
    for index in range(npts):
        inds = np.argsort(sims[index])[::-1]
        # Score
        ranks[index] = np.where(inds == index)[0][0]
        top1[index] = inds[0]
        #if ranks[index] != 0:
        #    fw.write(str(index)+'\t'+str(i)+'\t'+str(ranks[index])+'\t'+str(top1[index])+'\t'+str(sims[index][i])+'\t'+str(sims[index][int(inds[0])])+'\n')
    #fw.close()

    # Compute metrics
    r1 = 100.0 * len(np.where(ranks < 1)[0]) / len(ranks)
    r5 = 100.0 * len(np.where(ranks < 5)[0]) / len(ranks)
    r10 = 100.0 * len(np.where(ranks < 10)[0]) / len(ranks)
    medr = np.floor(np.median(ranks)) + 1
    meanr = ranks.mean() + 1
    if return_ranks:
        return (r1, r5, r10, medr, meanr), (ranks, top1)
    else:
        return (r1, r5, r10, medr, meanr)


def t2i(images, captions, sims, npts=None, return_ranks=False):
    """
    Text->Images (Image Search)
    Images: (N, n_region, d) matrix of images
    Captions: (5N, max_n_word, d) matrix of captions
    CapLens: (5N) array of caption lengths
    sims: (N, 5N) matrix of similarity im-cap
    """
    print("Sims shape", sims.shape)
    npts = images.shape[0]
    ranks = np.zeros(npts)
    top1 = np.zeros(npts)

    # --> (5N(caption), N(image))
    sims = sims.T

    #fw = open('not_good_t2i.txt','w')
    #fw.write('Text\tImage\tRank\tTop1\tSim\tSim-top1\n')
    for index in range(npts): # index : [0,1000)
        inds = np.argsort(sims[index])[::-1]
        ranks[index] = np.where(inds == index)[0][0]
        top1[index] = inds[0]
    #fw.close()

    # Compute metrics
    r1 = 100.0 * len(np.where(ranks < 1)[0]) / len(ranks)
    r5 = 100.0 * len(np.where(ranks < 5)[0]) / len(ranks)
    r10 = 100.0 * len(np.where(ranks < 10)[0]) / len(ranks)
    medr = np.floor(np.median(ranks)) + 1
    meanr = ranks.mean() + 1
    if return_ranks:
        return (r1, r5, r10, medr, meanr), (ranks, top1)
    else:
        return (r1, r5, r10, medr, meanr)
