import socket
import timeit
from datetime import datetime
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '1'
import glob
from collections import OrderedDict
import numpy as np

# PyTorch includes
import torch
from torch.autograd import Variable
import torch.optim as optim
from torchvision import transforms
from torch.utils.data import DataLoader
from torchvision.utils import make_grid

# Tensorboard include
from tensorboardX import SummaryWriter

# Custom includes
from dataloaders import cityscapes
from dataloaders import utils
from networks import deeplab_xception, deeplab_resnet
from dataloaders import custom_transforms as tr

gpu_id = 0
print('Using GPU: {} '.format(gpu_id))
# Setting parameters
nEpochs = 200  # Number of epochs for training
resume_epoch = 90  # Default is 0, change if want to resume

BATCH_SIZE = 2

p = OrderedDict()  # Parameters to include in report
p['trainBatch'] = BATCH_SIZE  # Training batch size
testBatch = BATCH_SIZE  # Testing batch size
useTest = True  # See evolution of the test set when training
nValInterval = 5  # Run on test set every nTestInterval epochs
snapshot = 10  # Store a model every snapshot epochs
p['nAveGrad'] = 1  # Average the gradient of several iterations
p['lr'] = 1e-7  # Learning rate
p['wd'] = 5e-4  # Weight decay
p['momentum'] = 0.9  # Momentum
p['epoch_size'] = 10  # How many epochs to change learning rate
backbone = 'xception' # Use xception or resnet as feature extractor,

save_dir_root = os.path.join(os.path.dirname(os.path.abspath(__file__)))
exp_name = os.path.dirname(os.path.abspath(__file__)).split('/')[-1]

if resume_epoch != 0:
    runs = sorted(glob.glob(os.path.join(save_dir_root, 'run', 'run_*')))
    run_id = int(runs[-1].split('_')[-1]) + 1 if runs else 0
else:
    runs = sorted(glob.glob(os.path.join(save_dir_root, 'run', 'run_*')))
    run_id = int(runs[-1].split('_')[-1]) + 1 if runs else 0

save_dir = os.path.join(save_dir_root, 'run', 'run_' + str(run_id))

# Network definition
if backbone == 'xception':
    net = deeplab_xception.DeepLabv3_plus(nInputChannels=3, n_classes=19, os=16, pretrained=True)
elif backbone == 'resnet':
    net = deeplab_resnet.DeepLabv3_plus(nInputChannels=3, n_classes=19, os=16, pretrained=True)
else:
    raise NotImplementedError
modelName = 'deeplabv3plus-' + backbone + '-cityscapes'
criterion = utils.cross_entropy2d

if resume_epoch == 0:
    print("Training deeplabv3+ from scratch...")
else:
    print("Initializing weights from: {}...".format(
        os.path.join(save_dir, 'models', modelName + '_epoch-' + str(resume_epoch - 1) + '.pth')))
    net.load_state_dict(
        torch.load(os.path.join(save_dir, 'models', modelName + '_epoch-' + str(resume_epoch - 1) + '.pth'),
                   map_location=lambda storage, loc: storage))  # Load all tensors onto the CPU

if gpu_id >= 0:
    torch.cuda.set_device(device=gpu_id)
    net.cuda()

if resume_epoch != nEpochs:
    # Logging into Tensorboard
    log_dir = os.path.join(save_dir, 'models', datetime.now().strftime('%b%d_%H-%M-%S') + '_' + socket.gethostname())
    writer = SummaryWriter(log_dir=log_dir)

    # Use the following optimizer
    optimizer = optim.SGD(net.parameters(), lr=p['lr'], momentum=p['momentum'], weight_decay=p['wd'])
    p['optimizer'] = str(optimizer)

    composed_transforms_tr = transforms.Compose([
        tr.RandomHorizontalFlip(),
        tr.RandomScale((0.5, 0.75)),
        tr.RandomCrop((512, 1024)),
        tr.RandomRotate(5),
        tr.Normalize_cityscapes(mean=(72.39, 82.91, 73.16)),
        tr.ToTensor()])

    composed_transforms_ts = transforms.Compose([
        tr.RandomHorizontalFlip(),
        tr.Scale((819, 1638)),
        tr.CenterCrop((512, 1024)),
        tr.Normalize_cityscapes(mean=(72.39, 82.91, 73.16)),
        tr.ToTensor()])

    cityscapes_train = cityscapes.CityscapesSegmentation(split='train',
                                                         transform=composed_transforms_tr)
    cityscapes_val = cityscapes.CityscapesSegmentation(split='val',
                                                       transform=composed_transforms_ts)
    cityscapes_test = cityscapes.CityscapesSegmentation(split='test',
                                                        transform=composed_transforms_ts)

    trainloader = DataLoader(cityscapes_train, batch_size=p['trainBatch'], shuffle=True, num_workers=0)
    valloader = DataLoader(cityscapes_val, batch_size=testBatch, shuffle=True, num_workers=0)
    testloader = DataLoader(cityscapes_test, batch_size=testBatch, shuffle=False, num_workers=0)

    utils.generate_param_report(os.path.join(save_dir, exp_name + '.txt'), p)

    num_img_tr = len(trainloader)
    num_img_vl = len(valloader)
    num_img_ts = len(testloader)
    running_loss_tr = 0.0
    running_loss_vl = 0.0
    running_loss_ts = 0.0
    previous_miou = -1.0
    aveGrad = 0
    global_step = 0
    print("Training Network")

    # Main Training and Testing Loop
    for epoch in range(resume_epoch, nEpochs):
        start_time = timeit.default_timer()

        if epoch % p['epoch_size'] == p['epoch_size'] - 1:
            lr_ = utils.lr_poly(p['lr'], epoch, nEpochs, 0.9)
            print('(poly lr policy) learning rate: ', lr_)
            optimizer = optim.SGD(net.parameters(), lr=lr_, momentum=p['momentum'], weight_decay=p['wd'])

        net.train()
        for ii, sample_batched in enumerate(trainloader):

            inputs, labels = sample_batched['image'], sample_batched['label']
            if inputs.shape[0] != BATCH_SIZE:    continue
            # Forward-Backward of the mini-batch
            inputs, labels = Variable(inputs, requires_grad=True), Variable(labels)
            global_step += inputs.data.shape[0]

            if gpu_id >= 0:
                inputs, labels = inputs.cuda(), labels.cuda()

            outputs = net.forward(inputs)

            loss = criterion(outputs, labels, size_average=False, batch_average=True)
            running_loss_tr += loss.item()

            # Print stuff
            if ii % num_img_tr == (num_img_tr - 1):
                running_loss_tr = running_loss_tr / num_img_tr
                writer.add_scalar('data/total_loss_epoch', running_loss_tr, epoch)
                print('[Epoch: %d, numImages: %5d]' % (epoch, ii * p['trainBatch'] + inputs.data.shape[0]))
                print('Loss: %f' % running_loss_tr)
                running_loss_tr = 0
                stop_time = timeit.default_timer()
                print("Execution time: " + str(stop_time - start_time) + "\n")

            # Backward the averaged gradient
            loss /= p['nAveGrad']
            loss.backward()
            aveGrad += 1

            # Update the weights once in p['nAveGrad'] forward passes
            if aveGrad % p['nAveGrad'] == 0:
                writer.add_scalar('data/total_loss_iter', loss.item(), ii + num_img_tr * epoch)
                optimizer.step()
                optimizer.zero_grad()
                aveGrad = 0

            # Show 10 * 3 images results each epoch
            if ii % (num_img_tr // 10) == 0:
                grid_image = make_grid(inputs[:3].clone().cpu().data, 3, normalize=True)
                writer.add_image('Image', grid_image, global_step)
                grid_image = make_grid(
                    utils.decode_seg_map_sequence(torch.max(outputs[:3], 1)[1].detach().cpu().numpy(), 'cityscapes'), 3,
                    normalize=False,
                    range=(0, 255))
                writer.add_image('Predicted label', grid_image, global_step)
                grid_image = make_grid(
                    utils.decode_seg_map_sequence(torch.squeeze(labels[:3], 1).detach().cpu().numpy(), 'cityscapes'), 3,
                    normalize=False, range=(0, 255))
                writer.add_image('Groundtruth label', grid_image, global_step)

        # One testing epoch
        if epoch % nValInterval == (nValInterval - 1):
            total_miou = 0.0
            net.eval()
            for ii, sample_batched in enumerate(valloader):
                inputs, labels = sample_batched['image'], sample_batched['label']

                # Forward pass of the mini-batch
                inputs, labels = Variable(inputs, requires_grad=True), Variable(labels)
                if gpu_id >= 0:
                    inputs, labels = inputs.cuda(), labels.cuda()

                with torch.no_grad():
                    outputs = net.forward(inputs)

                predictions = torch.max(outputs, 1)[1]

                loss = criterion(outputs, labels, size_average=False, batch_average=True)
                running_loss_vl += loss.item()
                total_miou += utils.get_iou(predictions, labels, 19)

                # Print stuff
                if ii % num_img_vl == num_img_vl - 1:
                    miou = total_miou / (ii * testBatch + inputs.data.shape[0])
                    running_loss_vl = running_loss_vl / num_img_vl

                    print('Validation:')
                    print('[Epoch: %d, numImages: %5d]' % (epoch, ii * testBatch + inputs.data.shape[0]))
                    writer.add_scalar('data/test_loss_epoch', running_loss_vl, epoch)
                    writer.add_scalar('data/test_miour', miou, epoch)
                    print('Loss: %f' % running_loss_vl)
                    print('MIoU: %f\n' % miou)
                    running_loss_vl = 0

        # Save the model
        if (epoch % snapshot) == snapshot - 1 and miou > previous_miou:
            previous_miou = miou
            torch.save(net.state_dict(), os.path.join(save_dir, 'models', modelName + '_epoch-' + str(epoch) + '.pth'))
            print("Save model at {}\n".format(
                os.path.join(save_dir, 'models', modelName + '_epoch-' + str(epoch) + '.pth')))

    writer.close()

    if useTest:
        total_miou = 0.0
        net.eval()
        for ii, sample_batched in enumerate(testloader):
            inputs, labels = sample_batched['image'], sample_batched['label']

            # Forward pass of the mini-batch
            inputs, labels = Variable(inputs, requires_grad=True), Variable(labels)
            if gpu_id >= 0:
                inputs, labels = inputs.cuda(), labels.cuda()

            with torch.no_grad():
                outputs = net.forward(inputs)

            predictions = torch.max(outputs, 1)[1]

            loss = criterion(outputs, labels, size_average=False, batch_average=True)
            running_loss_ts += loss.item()

            total_miou += utils.get_iou(predictions, labels, 19)

            # Print stuff
            if ii % num_img_ts == num_img_ts - 1:
                miou = total_miou / (ii * testBatch + inputs.data.shape[0])
                running_loss_ts = running_loss_ts / num_img_ts

                print('Test:')
                print('Loss: %f' % running_loss_ts)
                print('MIoU: %f\n' % miou)
                running_loss_ts = 0
