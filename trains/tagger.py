import time

import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence

from models.encoders.tagger import EncoderTagger

from datasets.tag import TagDataset

from utils.checkpoint import save_tagger_checkpoint
from utils.device import get_device
from utils.metric import AverageMeter, binary_accuracy
from utils.optimizer import clip_gradient, adjust_learning_rate

# Data parameters
data_folder = './scn_data'  # folder with data files saved by create_input_files.py
# base name shared by data files
data_name = 'flickr10k_5_cap_per_img_5_min_word_freq'

# Model parameters
semantic_size = 1000
dropout = 0.15
# sets device for model and PyTorch tensors
device = get_device()
# set to true only if inputs to model are fixed size; otherwise lot of computational overhead
cudnn.benchmark = True

# Training parameters
start_epoch = 0
# number of epochs to train for (if early stopping is not triggered)
epochs = 10
# keeps track of number of epochs since there's been an improvement in validation BLEU
epochs_since_improvement = 0
batch_size = 32
adjust_lr_after_epoch = 4
fine_tune_encoder = False
workers = 1  # for data-loading; right now, only 1 works with h5py
encoder_lr = 1e-4  # learning rate for encoder if fine-tuning
grad_clip = 5.  # clip gradients at an absolute value of
best_acc = 0.  # Best acc right now
print_freq = 100  # print training/validation stats every __ batches
checkpoint = None  # path to checkpoint, None if none


def main():
    """
    Training and validation.
    """

    global best_acc, epochs_since_improvement, checkpoint, start_epoch, fine_tune_encoder, data_name

    print('Running on device {}\n'.format(device))

    # Initialize / load checkpoint
    if checkpoint is None:
        encoder = EncoderTagger(semantic_size=semantic_size, dropout=dropout)
        encoder.fine_tune(fine_tune_encoder)
        encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
                                             lr=encoder_lr)
    else:
        checkpoint = torch.load(checkpoint)
        start_epoch = checkpoint['epoch'] + 1
        epochs_since_improvement = checkpoint['epochs_since_improvement']
        best_acc = checkpoint['accuracy']
        encoder = checkpoint['encoder']
        encoder_optimizer = checkpoint['encoder_optimizer']
        encoder.fine_tune(fine_tune_encoder)
        encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
                                             lr=encoder_lr)

    # Move to GPU, if available
    encoder = encoder.to(device)

    # Loss function
    criterion = nn.BCELoss().to(device)

    # Custom dataloaders
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])
    train_loader = torch.utils.data.DataLoader(
        TagDataset(data_folder, data_name, 'TRAIN',
                   transform=transforms.Compose([normalize])),
        batch_size=batch_size, shuffle=True, num_workers=workers, pin_memory=True)
    val_loader = torch.utils.data.DataLoader(
        TagDataset(data_folder, data_name, 'VAL',
                   transform=transforms.Compose([normalize])),
        batch_size=batch_size, shuffle=True, num_workers=workers, pin_memory=True)

    # Epochs
    for epoch in range(start_epoch, epochs):
        print('Current epoch {}\n'.format(epoch + 1))

        # Decay learning rate if there is no improvement for 8 consecutive epochs, and terminate training after 20
        if epochs_since_improvement == 20:
            break
        if epochs_since_improvement > 0 and epochs_since_improvement % adjust_lr_after_epoch == 0:
            adjust_learning_rate(encoder_optimizer, 0.8)

        # One epoch's training
        train(train_loader=train_loader,
              encoder=encoder,
              criterion=criterion,
              encoder_optimizer=encoder_optimizer,
              epoch=epoch)

        # One epoch's validation
        acc = validate(val_loader=val_loader,
                       encoder=encoder,
                       criterion=criterion)

        # Check if there was an improvement
        is_best = acc.avg > best_acc
        best_acc = max(acc.avg, best_acc)
        if not is_best:
            epochs_since_improvement += 1
            print("\nEpochs since last improvement: %d\n" %
                  (epochs_since_improvement,))
        else:
            epochs_since_improvement = 0

        print('Saving checkpoint for epoch {}\n'.format(epoch + 1))

        # Save checkpoint
        save_tagger_checkpoint(data_name, epoch, epochs_since_improvement, encoder, encoder_optimizer,
                               acc, is_best)


def train(train_loader, encoder, criterion, encoder_optimizer, epoch):
    r"""Performs one epoch's training.

    Arguments
        train_loader: DataLoader for training data
        encoder: encoder model
        criterion: loss layer
        encoder_optimizer: optimizer to update encoder's weights
        epoch: epoch number
    """

    encoder.train()

    batch_time = AverageMeter()  # forward prop. + back prop. time
    data_time = AverageMeter()  # data loading time
    losses = AverageMeter()  # loss (per word decoded)
    accs = AverageMeter()  # acc accuracy

    start = time.time()

    # Batches
    for i, (imgs, tags) in enumerate(train_loader):
        data_time.update(time.time() - start)

        # Move to GPU, if available
        imgs = imgs.to(device)
        targets = tags.to(device)

        # Forward prop.
        scores = encoder(imgs)
        # Calculate loss
        loss = criterion(scores, targets)

        # Back prop.
        encoder_optimizer.zero_grad()
        loss.backward()

        # Clip gradients
        clip_gradient(encoder_optimizer, grad_clip)

        # Update weights
        encoder_optimizer.step()

        # Keep track of metrics
        acc = binary_accuracy(scores, targets)
        losses.update(loss.item())
        accs.update(acc)
        batch_time.update(time.time() - start)

        start = time.time()

        # Print status
        if i % print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Batch Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data Load Time {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Top-5 Accuracy {accs.val:.3f} ({accs.avg:.3f})'.format(epoch, i, len(train_loader),
                                                                          batch_time=batch_time,
                                                                          data_time=data_time, loss=losses,
                                                                          accs=accs))


def validate(val_loader, encoder, criterion):
    r"""Performs one epoch's validation.

    Arguments
        val_loader (Generator): DataLoader for validation data.
        encoder (nn.Module): encoder model
        criterion: loss layer
    Returns
        AverageMeter: Accuracy
    """

    encoder.eval()

    batch_time = AverageMeter()
    losses = AverageMeter()
    accs = AverageMeter()

    start = time.time()

    # explicitly disable gradient calculation to avoid CUDA memory error
    # solves the issue #57
    with torch.no_grad():
        # Batches
        for i, (imgs, tags) in enumerate(val_loader):

            # Move to device, if available
            imgs = imgs.to(device)
            targets = tags.to(device)

            # Forward prop.
            scores = encoder(imgs)

            # Calculate loss
            loss = criterion(scores, targets)

            # Keep track of metrics
            losses.update(loss.item())
            acc = binary_accuracy(scores, targets)
            accs.update(acc)
            batch_time.update(time.time() - start)

            start = time.time()

            if i % print_freq == 0:
                print('Validation: [{0}/{1}]\t'
                      'Batch Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Accuracy {accs.val:.3f} ({accs.avg:.3f})\t'.format(i, len(val_loader), batch_time=batch_time,
                                                                          loss=losses, accs=accs))

        print(
            '\n * LOSS - {loss.avg:.3f}, ACCURACY - {acc.avg:.3f}\n'.format(
                loss=losses,
                acc=accs))

    return accs


if __name__ == '__main__':
    main()
