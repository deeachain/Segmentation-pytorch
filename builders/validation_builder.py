import numpy as np
import torch
import matplotlib.pyplot as plt
from scipy import ndimage
from math import ceil
from tqdm import tqdm
from utils.utils import save_predict
from utils.metric.SegmentationMetric import SegmentationMetric


def pad_image(img, target_size):
    """Pad an image up to the target size."""
    rows_missing = target_size[0] - img.shape[2]  # 512-512 = 0
    cols_missing = target_size[1] - img.shape[3]  # 512-512 = 0
    # 在右、下边用0padding
    padded_img = np.pad(img, ((0, 0), (0, 0), (0, rows_missing), (0, cols_missing)), 'constant')
    return padded_img  # shape(1,3,512,512)


def pad_label(img, target_size):
    """Pad an image up to the target size."""
    rows_missing = target_size[0] - img.shape[1]  # 512-512 = 0
    cols_missing = target_size[1] - img.shape[2]  # 512-512 = 0
    # 在右、下边用0padding
    padded_img = np.pad(img, ((0, 0), (0, rows_missing), (0, cols_missing)), 'constant')
    return padded_img  # shape(1,512,512)


# 滑动窗口法
# image.shape(1,3,1024,2048)、tile_size=(512,512)、classes=3
# image:需要预测的图片(1,3,3072,3328);tile_size:小方块大小;
def predict_sliding(args, model, testLoader, tile_size, criteria, mode='predict'):
    total_batches = len(testLoader)
    val_loss = 0
    metric = SegmentationMetric(args.classes)  # args.classes表示有args.classes个分类
    pbar = tqdm(iterable=enumerate(testLoader), total=total_batches, desc='Predicting')
    for i, (image, gt, size, name) in pbar:
        image_size = image.shape  # (1,3,3328,3072)
        overlap = 1 / 3  # 每次滑动的覆盖率为1/3
        stride = ceil(tile_size[0] * (1 - overlap))  # 滑动步长:512*(1-1/3) = 513     512*(1-1/3)= 342
        tile_rows = int(ceil((image_size[2] - tile_size[0]) / stride) + 1)  # 行滑动步数:(3072-512)/342+1=9
        tile_cols = int(ceil((image_size[3] - tile_size[1]) / stride) + 1)  # 列滑动步数:(3328-512)/342+1=10
        full_probs = np.zeros((image_size[2], image_size[3], args.classes))  # 初始化全概率矩阵shape(3072,3328,3)
        count_predictions = np.zeros((image_size[2], image_size[3], args.classes))  # 初始化计数矩阵shape(3072,3328,3)

        for row in range(tile_rows):  # row = 0,1     0,1,2,3,4,5,6,7,8
            for col in range(tile_cols):  # col = 0,1,2,3     0,1,2,3,4,5,6,7,8,9
                x1 = int(col * stride)  # 起始位置x1 = 0 * 513 = 0   0*342
                y1 = int(row * stride)  # y1 = 0 * 513 = 0   0*342
                x2 = min(x1 + tile_size[1], image_size[3])  # 末位置x2 = min(0+512, 3328)
                y2 = min(y1 + tile_size[0], image_size[2])  # y2 = min(0+512, 3072)
                x1 = max(int(x2 - tile_size[1]), 0)  # 重新校准起始位置x1 = max(512-512, 0)
                y1 = max(int(y2 - tile_size[0]), 0)  # y1 = max(512-512, 0)

                img = image[:, :, y1:y2, x1:x2]  # 滑动窗口对应的图像 imge[:, :, 0:512, 0:512]
                label = gt[:, y1:y2, x1:x2]
                padded_img = pad_image(img, tile_size)  # padding 确保扣下来的图像为512*512
                padded_label = pad_label(label, tile_size)
                # plt.imshow(padded_img)
                # plt.show()

                # 将扣下来的部分传入网络，网络输出概率图。
                with torch.no_grad():
                    image_var = torch.from_numpy(padded_img).cuda().float()
                    padded_prediction = model(image_var)
                    if type(padded_prediction) is tuple:
                        padded_prediction = padded_prediction[0]
                    torch.cuda.synchronize()
                    if isinstance(padded_prediction, list):
                        padded_prediction = padded_prediction[0]

                    if mode == 'validation':
                        padded_label = torch.from_numpy(padded_label).long().cuda()
                        val_loss = criteria(padded_prediction, padded_label).cuda()
                padded_prediction = padded_prediction.cpu().data[0].numpy().transpose(1, 2, 0)  # 通道位置变换(512,512,3)
                prediction = padded_prediction[0:img.shape[2], 0:img.shape[3], :]  # 扣下相应面积 shape(512,512,3)
                count_predictions[y1:y2, x1:x2] += 1  # 窗口区域内的计数矩阵加1
                full_probs[y1:y2, x1:x2] += prediction  # 窗口区域内的全概率矩阵叠加预测结果

        # average the predictions in the overlapping regions
        full_probs /= count_predictions  # 全概率矩阵 除以 计数矩阵 即得 平均概率
        # visualize normalization Weights
        # plt.imshow(np.mean(count_predictions, axis=2))
        # plt.show()
        full_probs = np.asarray(np.argmax(full_probs, axis=2), dtype=np.uint8)
        '''设置输出原图和预测图片的颜色灰度还是彩色'''
        # plt.imshow(gt[0])
        # plt.show()
        gt = gt[0].numpy()  # gt shape is (batch_size, h, w)
        # 计算miou
        metric.addBatch(full_probs, gt)
        save_predict(full_probs, gt, name[0], args.dataset, args.save_seg_dir,
                     output_grey=False, output_color=True, gt_color=True)

    pa = metric.pixelAccuracy()
    mpa, cpa = metric.meanPixelAccuracy()
    Miou, PerMiou_set = metric.meanIntersectionOverUnion()
    FWIoU = metric.Frequency_Weighted_Intersection_over_Union()

    result = args.save_seg_dir + '/results.txt'
    with open(result, 'w') as f:
        f.write(str(Miou))
        f.write('\n{}'.format(PerMiou_set))

    return val_loss, FWIoU, Miou, PerMiou_set, pa, cpa, mpa


# model output is feature map without Up sampling
def predict_whole(args, model, testLoader, criteria, mode='predict'):
    """
    model:  model
    image:  test image
    tile_size:  predict output image size
    return:  predict output
    """
    total_batches = len(testLoader)
    val_loss = 0
    metric = SegmentationMetric(args.classes)  # args.classes表示有args.classes个分类
    pbar = tqdm(iterable=enumerate(testLoader), total=total_batches, desc='Predicting')
    for i, (image, gt, size, name) in pbar:
        with torch.no_grad():
            predict = model(image.cuda())
            if mode == 'validation':
                val_loss = criteria(predict, gt.long().cuda()).cuda()

            gt = gt[0].numpy()
            predict = predict.cpu().data[0].numpy().transpose(1, 2, 0)  # 通道位置变换(512,512,3)
            predict = np.asarray(np.argmax(predict, axis=2), dtype=np.uint8)
            save_predict(predict, gt, name[0], args.dataset, args.save_seg_dir,
                         output_grey=False, output_color=True, gt_color=True)

            metric.addBatch(predict, gt)
            pa = metric.pixelAccuracy()
            mpa, cpa = metric.meanPixelAccuracy()
            Miou, PerMiou_set = metric.meanIntersectionOverUnion()
            FWIoU = metric.Frequency_Weighted_Intersection_over_Union()

    return val_loss, FWIoU, Miou, PerMiou_set, pa, cpa, mpa


def predict_multiscale(model, image, tile_size, scales, classes, flip_evaluation):
    """
        Predict an image by looking at it with different scales.
        We choose the "predict_whole_img" for the image with less than the original input size,
        for the input of larger size, we would choose the cropping method to ensure that GPU memory is enough.
    """
    image = image.data
    N_, C_, H_, W_ = image.shape
    full_probs = np.zeros((H_, W_, classes))
    for scale in scales:
        scale = float(scale)
        print("Predicting image scaled by %f" % scale)
        scale_image = ndimage.zoom(image, (1.0, 1.0, scale, scale), order=1, prefilter=False)
        scaled_probs = predict_whole(model, scale_image, tile_size)
        if flip_evaluation == True:
            flip_scaled_probs = predict_whole(model, scale_image[:, :, :, ::-1].copy(), tile_size)
            scaled_probs = 0.5 * (scaled_probs + flip_scaled_probs[:, ::-1, :])
        full_probs += scaled_probs
    full_probs /= len(scales)
    return full_probs
