import matplotlib as mpl

# This line allows mpl to run with no DISPLAY defined
mpl.use('Agg')

import pandas as pd
import numpy as np
import os
from keras.layers import Reshape, Flatten, LeakyReLU, Activation, Conv2D, Dense
from keras.layers.convolutional import UpSampling2D, MaxPooling2D
from keras.models import Sequential
from keras.optimizers import Adam
from keras.callbacks import CSVLogger
from keras_adversarial.image_grid_callback import ImageGridCallback
import h5py
from keras.preprocessing.image import ImageDataGenerator
import sys
sys.setrecursionlimit(10000)
sys.path.append("..")
from util.data import iterate_hdf5, Hdf5DcganIterator

from keras_adversarial import AdversarialModel, simple_gan, gan_targets
from keras_adversarial import AdversarialOptimizerSimultaneous, normal_latent_sampling
from keras_adversarial.adversarial_utils import uniform_latent_sampling
from keras_adversarial.legacy import BatchNormalization, fit, fit_generator, l1l2, AveragePooling2D
import keras.backend as K
from image_utils import dim_ordering_fix, dim_ordering_unfix, dim_ordering_shape


def _model_generator():
    model = Sequential()
    nch = 256
    reg = lambda: l1l2(l1=1e-7, l2=1e-7)
    h = 5
    model.add(Dense(nch * 4 * 4, input_dim=100, W_regularizer=reg()))
    model.add(BatchNormalization(mode=0))
    model.add(Reshape(dim_ordering_shape((nch, 4, 4))))
    model.add(Convolution2D(nch / 2, h, h, border_mode='same', W_regularizer=reg()))
    model.add(BatchNormalization(mode=0, axis=1))
    model.add(LeakyReLU(0.2))
    model.add(UpSampling2D(size=(2, 2)))
    model.add(Convolution2D(nch / 2, h, h, border_mode='same', W_regularizer=reg()))
    model.add(BatchNormalization(mode=0, axis=1))
    model.add(LeakyReLU(0.2))
    model.add(UpSampling2D(size=(2, 2)))
    model.add(Convolution2D(nch / 4, h, h, border_mode='same', W_regularizer=reg()))
    model.add(BatchNormalization(mode=0, axis=1))
    model.add(LeakyReLU(0.2))
    model.add(UpSampling2D(size=(2, 2)))
    model.add(Convolution2D(3, h, h, border_mode='same', W_regularizer=reg()))
    model.add(Activation('sigmoid'))
    return model


def model_generator(latent_dim, nch=512, h=5, initial_size=4, final_size=512, div=[2,2,4,4,8,8,16], num_repeats=0, reg=lambda: l1l2(l1=1e-7, l2=1e-7)):
    # e.g. for 512x512 generation, if we start at 4x4,
    # we are only allowed 7 upsampling div to take us to
    # 512px
    assert initial_size * (2**len(div)) == final_size
    model = Sequential()
    model.add(Dense(nch * initial_size * initial_size, input_dim=latent_dim, kernel_regularizer=reg()))
    model.add(BatchNormalization(axis=1)) ##?
    model.add(Reshape(dim_ordering_shape((nch, initial_size, initial_size))))
    div = [nch/elem for elem in div]
    print div
    #div = [nch/2, nch/2, nch/4, nch/4, nch/8, nch/8, nch/16]
    for n in div:
        for r in range(num_repeats+1):
            model.add(Conv2D(n, h, padding='same', kernel_regularizer=reg()))
            model.add(BatchNormalization(mode=0, axis=1))
            model.add(LeakyReLU(0.2))
        model.add(UpSampling2D(size=(2, 2)))
    model.add(Conv2D(1, h, padding='same', kernel_regularizer=reg()))
    model.add(Activation('sigmoid'))
    return model

def model_discriminator(nch=512, h=5, div=[8,4,4,2,2,1,1], num_repeats=0, bn=False, pool_mode='max', reg=lambda: l1l2(l1=1e-7, l2=1e-7) ):
    model = Sequential()
    n = nch / 8
    #div = [nch/8, nch/4, nch/4, nch/2, nch/2, nch/1, nch/1]
    div = [nch/elem for elem in div]
    for idx,n in enumerate(div):
        for r in range(num_repeats+1):
            if idx==0:
                model.add(Conv2D(n, h, strides=1, padding='same', kernel_regularizer=reg(),
                               input_shape=dim_ordering_shape((1, 512, 512))))
            else:
                model.add(Conv2D(n, h, strides=1, padding='same', kernel_regularizer=reg()))
            if bn:
                model.add(BatchNormalization(mode=0,axis=1))
            model.add(LeakyReLU(0.2))
        if pool_mode == 'max':
            model.add(MaxPooling2D(pool_size=(2, 2)))
        else:
            model.add(AveragePooling2D(pool_size=(2,2)))
    model.add(Conv2D(1, h, padding='same', kernel_regularizer=reg()))
    reduction_factor = nch // (2**len(div))
    model.add(AveragePooling2D(pool_size=(reduction_factor,reduction_factor), border_mode='valid')) #4x4
    model.add(Flatten())
    model.add(Activation('sigmoid'))
    return model


def train(adversarial_optimizer, path, opt_g, opt_d, nb_epoch, generator, discriminator, iterators, latent_dim=100, batch_size=4, loss='binary_crossentropy',
          latent_sampler=normal_latent_sampling, latent_sampler_args={}, resume=None):
    csvpath = os.path.join(path, "history.csv")
    if not os.path.exists(csvpath):
        os.makedirs(csvpath)
    
    print("Training: {}".format(csvpath))
    # gan (x - > yfake, yreal), z is gaussian generated on GPU
    # can also experiment with uniform_latent_sampling
    
    generator.summary()
    discriminator.summary()

    from keras.utils import plot_model
    for model, name in zip([generator, discriminator], ["gen", "disc"]):
        plot_model(model, show_shapes=True, to_file="%s/graph_%s.png" % (path, name))
    
    latent_sampler = latent_sampler( (latent_dim,), **latent_sampler_args )
    
    gan = simple_gan(generator=generator,
                     discriminator=discriminator,
                     latent_sampling=latent_sampler)

    # build adversarial model
    model = AdversarialModel(base_model=gan,
                             player_params=[generator.trainable_weights, discriminator.trainable_weights],
                             player_names=["generator", "discriminator"])
    model.adversarial_compile(adversarial_optimizer=adversarial_optimizer,
                              player_optimizers=[opt_g, opt_d],
                              loss=loss)

    # create callback to generate images
    zsamples = np.random.normal(size=(10 * 10, latent_dim))

    def generator_sampler():
        #import pdb
        #pdb.set_trace()
        # [:,:,:,0] since this is a b/w image
        xpred = dim_ordering_unfix(generator.predict(zsamples)).transpose((0, 2, 3, 1))[:,:,:,0]
        return xpred.reshape((10, 10) + xpred.shape[1:])

    generator_cb = ImageGridCallback(os.path.join(path, "epoch-{:03d}.png"), generator_sampler, cmap='gray')

    callbacks = [generator_cb]
    csv_filename = "%s/results.txt" % path
    if resume == None:
        # create the file
        f = open(csv_filename, "wb")
        f.close()
    csv_logger = CSVLogger(csv_filename, append=True if os.path.exists(csv_filename) else False)
    callbacks.append(csv_logger)    
    
    #history = fit(model, x=dim_ordering_fix(xtrain), y=y, validation_data=(dim_ordering_fix(xtest), ytest),
    #              callbacks=callbacks, nb_epoch=nb_epoch,
    #              batch_size=32)
    it_train, it_val = iterators
    model.fit_generator( it_train, steps_per_epoch=it_train.N, epochs=nb_epoch, callbacks=callbacks)
    
    # save history to CSV
    df = pd.DataFrame(history.history)
    df.to_csv(csvpath)

    # save models
    generator.save(os.path.join(path, "generator.h5"))
    discriminator.save(os.path.join(path, "discriminator.h5"))


def get_iterators(batch_size):
    dataset = h5py.File("/data/lisa/data/cbeckham/textures_v2_brown500.h5","r")
    imgen = ImageDataGenerator(horizontal_flip=True, vertical_flip=True, rotation_range=360, fill_mode="reflect")
    it_train = Hdf5DcganIterator(dataset['xt'], batch_size, imgen, is_binary=True)
    it_val = Hdf5DcganIterator(dataset['xv'], batch_size, imgen, is_binary=True)
    return it_train, it_val    

if __name__ == "__main__":

    def gan_heightmap_ld1000_bigd(mode):
        latent_dim = 1000
        generator = model_generator(latent_dim, num_repeats=0)
        discriminator = model_discriminator(num_repeats=0)
        if mode == "train":
            train(AdversarialOptimizerSimultaneous(), "output/gan-heightmap-ld1000-b",
                        opt_g=Adam(1e-4, decay=1e-5),
                        opt_d=Adam(1e-3, decay=1e-5),
                        nb_epoch=100, generator=generator, discriminator=discriminator,
                        latent_dim=latent_dim)

    def gan_heightmap_ld1000_b_discbn(mode):
        # this works well! so far anyway...
        latent_dim = 1000
        generator = model_generator(latent_dim, num_repeats=0)
        discriminator = model_discriminator(num_repeats=0, bn=True)
        if mode == "train":
            train(AdversarialOptimizerSimultaneous(), "output/gan-heightmap-ld1000-b-discbn",
                        opt_g=Adam(1e-4, decay=1e-5),
                        opt_d=Adam(1e-3, decay=1e-5),
                        nb_epoch=300, generator=generator, discriminator=discriminator,
                        latent_dim=latent_dim)            

    def gan_heightmap_ld1000_b_discbn_i1(mode):
        # this works well! so far anyway...
        latent_dim = 1000
        generator = model_generator(latent_dim, num_repeats=0, div=[2,2,4,4,8,8,8]) # not 16 at end
        discriminator = model_discriminator(num_repeats=0, bn=True)
        if mode == "train":
            train(AdversarialOptimizerSimultaneous(), "output/gan-heightmap-ld1000-b-discbn_i1",
                        opt_g=Adam(1e-4, decay=1e-5),
                        opt_d=Adam(1e-3, decay=1e-5),
                        nb_epoch=300, generator=generator, discriminator=discriminator,
                        latent_dim=latent_dim, iterators=get_iterators(4))


            
    def gan_heightmap_ld1000_b_discbn_bb1(mode):
        # may do well...
        latent_dim = 1000
        generator = model_generator(latent_dim, num_repeats=1)
        discriminator = model_discriminator(num_repeats=1, bn=True, div=[4,4,4,2,2,2,1])
        if mode == "train":
            train(AdversarialOptimizerSimultaneous(), "output/gan-heightmap-ld1000-b-discbn-bb1",
                        opt_g=Adam(1e-4, decay=1e-5),
                        opt_d=Adam(1e-3, decay=1e-5),
                        nb_epoch=300, generator=generator, discriminator=discriminator,
                        latent_dim=latent_dim, iterators=get_iterators(2))

    def gan_heightmap_ld1000_b_discbn_bb2(mode):
        # disc has slightly higher capacity than bb1
        latent_dim = 1000
        generator = model_generator(latent_dim, num_repeats=1)
        discriminator = model_discriminator(num_repeats=1, bn=True, div=[4,4,4,4,2,2,2,1])
        if mode == "train":
            train(AdversarialOptimizerSimultaneous(), "output/gan-heightmap-ld1000-b-discbn-bb2",
                        opt_g=Adam(1e-4, decay=1e-5),
                        opt_d=Adam(1e-3, decay=1e-5),
                        nb_epoch=300, generator=generator, discriminator=discriminator,
                        latent_dim=latent_dim, iterators=get_iterators(2))

    def gan_heightmap_ld1000_b_discbn_bb2b(mode):
        # disc has slightly higher capacity than bb1
        # but also equal LRs
        latent_dim = 1000
        generator = model_generator(latent_dim, num_repeats=1)
        discriminator = model_discriminator(num_repeats=1, bn=True, div=[4,4,4,4,2,2,2,1])
        if mode == "train":
            train(AdversarialOptimizerSimultaneous(), "output/gan-heightmap-ld1000-b-discbn-bb2b",
                        opt_g=Adam(1e-3, decay=1e-5),
                        opt_d=Adam(1e-3, decay=1e-5),
                        nb_epoch=300, generator=generator, discriminator=discriminator,
                        latent_dim=latent_dim, iterators=get_iterators(3))


            

            
    def gan_heightmap_ld1000_b_discbn_unif(mode):
        # try out unif sampling
        # https://arxiv.org/pdf/1706.00082.pdf
        # ^ those guys also limit the range of the uniform to control for
        # artifacts... may be useful
        latent_dim = 1000
        generator = model_generator(latent_dim, num_repeats=0)
        discriminator = model_discriminator(num_repeats=0, bn=True)
        if mode == "train":
            train(AdversarialOptimizerSimultaneous(), "output/gan-heightmap-ld1000-b-discbn-unif",
                        opt_g=Adam(1e-4, decay=1e-5),
                        opt_d=Adam(1e-3, decay=1e-5),
                        nb_epoch=100, generator=generator, discriminator=discriminator,
                        latent_dim=latent_dim,
                        latent_sampler=uniform_latent_sampling((latent_dim,)))


            
    locals()[ sys.argv[1] ]( sys.argv[2] )
