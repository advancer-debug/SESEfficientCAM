from sklearn.preprocessing import MinMaxScaler
import keras
from keras.applications.resnet50 import ResNet50
from keras.layers import Dense, Flatten, Dropout, GlobalAveragePooling2D, Concatenate, Input, Lambda, Multiply
from keras import backend as K
from keras.optimizers import SGD, Adam
import pandas as pd
import os
from tqdm import tqdm as tqdmn
from keras.preprocessing.image import ImageDataGenerator
from keras.preprocessing.balanced_image import BalancedImageDataGenerator
import numpy as np
from keras.callbacks import ModelCheckpoint, EarlyStopping, ReduceLROnPlateau, TensorBoard, CSVLogger
from keras import metrics
from keras import backend as K
from keras.models import Model, load_model
import csv
from sklearn.metrics import confusion_matrix, classification_report
from time import time
import multiprocessing
from joblib import Parallel, delayed
from skimage import io
from efficientnet import EfficientNetB0 as EfficientNet
import geopandas as gpd
import sys
sys.path.append("/warehouse/COMPLEXNET/jlevyabi/SATELSES/equirect_proj_test/cnes/python_files/aerial/")
from aerial_training_utils import generate_full_idINSPIRE, geographical_boundaries, my_preprocessor, fmeasure,recall,precision, fbeta_score
import argparse
import pickle
from scipy.special import binom
from sklearn.model_selection import StratifiedKFold

# Global paths
BASE_DIR = "/warehouse/COMPLEXNET/jlevyabi/"
CENSUS_DIR = BASE_DIR + 'REPLICATE_LINGSES/data_files/census_data/'
UA_DIR = BASE_DIR + "SATELSES/equirect_proj_test/cnes/data_files/land_ua_esa/FR/"
MODEL_OUTPUT_DIR = BASE_DIR + "SATELSES/equirect_proj_test/cnes/data_files/outputs/model_data/efficientnet_keras/2019_income_norm_v2/hyperparametrizations/"
BASE_AERDIR = BASE_DIR + "SATELSES/"
AERIAL_DIR= BASE_AERDIR + "equirect_proj_test/cnes/data_files/outputs/AERIAL_esa_URBAN_ATLAS_FR/"


print("Parsing Arguments...")
parser = argparse.ArgumentParser()
parser.add_argument('-city','--city',help = 'City to study')
parser.add_argument('-lr','--learning_rate', help = 'Learning Rate', type=float, default=8e-5)
parser.add_argument('-epochs','--num_epochs', help = '# Epochs', type=int, default=30)
parser.add_argument('-spe','--samples_per_epoch',help = '# Samples Per Epoch', type=int, default=250)
parser.add_argument('-lr_pat','--learning_rate_patience',help = '# Epochs to wait to diminish the lr', type=int,default=2)
parser.add_argument('-lr_dec','--learning_rate_decay',help = 'Decay with which to diminish the lr', type=float,default=.25)
parser.add_argument('-cv','--cv_folds',help = '# folds for cross-validation', type=int,default=5)

# Global variables
args = parser.parse_args()
city = args.city
PATIENCE_BEFORE_LOWERING_LR = args.learning_rate_patience
MAX_EPOCH = args.num_epochs
INITIAL_LR = args.learning_rate
CV_FOLDS = args.cv_folds
NB_SAMPLES_EPOCHS = args.samples_per_epoch
LR_DECAY = args.learning_rate_decay

MODEL_OUTPUT_DIR = "{}_{}_lr_{}_lr_dec_{}_eps_{}_spe_{}_cv_{}/".format(
    MODEL_OUTPUT_DIR,city,INITIAL_LR,LR_DECAY,MAX_EPOCH,NB_SAMPLES_EPOCHS,CV_FOLDS)


NB_SES_CLASSES = 5
PATIENCE_BEFORE_STOPPING = 25 #16 #8
TRAIN_TEST_FRAC = .8
VAL_SPLIT = .25
BATCH_SIZE = 10 #(real max) 16
IMG_SIZE = (800, 800)
INPUT_SHAPE = (IMG_SIZE[0], IMG_SIZE[1], 3)
CPU_COUNT = multiprocessing.cpu_count()
CPU_FRAC = .7
CPU_USE = int(CPU_FRAC*CPU_COUNT)
ADRIAN_ALBERT_THRESHOLD = .25
INSEE_AREA = 200*200

print("Generating Income Classes")
full_im_df_ua = generate_full_idINSPIRE(UA_DIR, AERIAL_DIR, NB_SES_CLASSES, ADRIAN_ALBERT_THRESHOLD, INSEE_AREA, old=False)
city_assoc = pd.read_csv(AERIAL_DIR + "city_assoc.csv")
full_im_df_ua = pd.merge(full_im_df_ua,city_assoc,on="idINSPIRE");
full_im_df_ua = full_im_df_ua[full_im_df_ua.FUA_NAME == city]

if not os.path.isdir(MODEL_OUTPUT_DIR):
    os.mkdir(MODEL_OUTPUT_DIR);
    os.mkdir(MODEL_OUTPUT_DIR+"logs/");

val_min = lambda x : np.percentile(x,0)
val_per20 = lambda x : np.percentile(x,20)
val_per40 = lambda x : np.percentile(x,40)
val_per60 = lambda x : np.percentile(x,60)
val_per80 = lambda x : np.percentile(x,80)
val_max = lambda x : np.percentile(x,100)

val_min.__name__ = 'qmin'
val_per20.__name__ = 'q20'
val_per40.__name__ = 'q40'
val_per60.__name__ = 'q60'
val_per80.__name__ = 'q80'
val_max.__name__ = 'qmax'

ses_city_intervals = full_im_df_ua.groupby("FUA_NAME")[["income"]].agg(
    [val_min,val_per20,val_per40,val_per60,val_per80,val_max]
)
df_cities = []
for city in list(ses_city_intervals.index):
    city_df_new = full_im_df_ua[full_im_df_ua.FUA_NAME==city]
    city_df_new.dropna(subset=["income"],inplace=True)
    income = city_df_new.income
    class_thresholds = ses_city_intervals.ix[city]["income"].values
    x_to_class = np.digitize(income,class_thresholds)
    x_to_class[x_to_class==np.max(x_to_class)] = NB_SES_CLASSES
    city_df_new["treated_citywise_income"] = [ str(y-1) for y in x_to_class ] 
    df_cities.append(city_df_new)

full_im_df_ua = gpd.GeoDataFrame(pd.concat(df_cities,axis=0),
                                            crs=full_im_df_ua.crs).sort_index()

print("Generating Generators")
full_im_df_ua = full_im_df_ua.sample(frac=1).reset_index(drop=True)
skf = StratifiedKFold(n_splits=CV_FOLDS)

for fold_id,(train_index, test_index) in enumerate(skf.split(full_im_df_ua.path2im, full_im_df_ua.treated_citywise_income)):
    #
    print("City: {} Fold {}/{}".format(city,fold_id+1,CV_FOLDS))
    train_im_df = full_im_df_ua.iloc[train_index]
    test_im_df = full_im_df_ua.iloc[test_index]
    train_test_split = train_im_df.shape[0]
    train_image_count = int(train_test_split*(1-VAL_SPLIT))
    val_image_count = int(train_test_split*VAL_SPLIT)
    test_image_count = test_im_df.shape[0]

    train_datagen = BalancedImageDataGenerator(preprocessing_function=my_preprocessor,
                                               horizontal_flip=True,validation_split=VAL_SPLIT,vertical_flip=True)
    test_datagen = ImageDataGenerator(preprocessing_function=my_preprocessor)

    train_generator = train_datagen.flow_from_dataframe(
            train_im_df,
            directory=AERIAL_DIR,
            x_col="path2im",
            y_col="treated_citywise_income",
            target_size=IMG_SIZE,
            color_mode ="rgb",
            shuffle=True,
            batch_size=BATCH_SIZE,
            interpolation="bicubic",
            subset="training",
            class_mode='categorical')

    val_generator = train_datagen.flow_from_dataframe(
            dataframe=train_im_df,
            directory=AERIAL_DIR,
            x_col="path2im",
            y_col="treated_citywise_income",
            target_size=IMG_SIZE,
            color_mode ="rgb",
            shuffle=True,
            batch_size=BATCH_SIZE,
            interpolation="bicubic",
            subset="validation",
            class_mode='categorical')

    test_generator = test_datagen.flow_from_dataframe(
            dataframe=test_im_df,
            directory=AERIAL_DIR,
            x_col="path2im",
            y_col="treated_citywise_income",
            target_size=IMG_SIZE,
            color_mode ="rgb",
            shuffle=False,
            batch_size=1,
            interpolation="bicubic",
            class_mode='categorical')

    print("Defining and Compiling Model")
    base_model = EfficientNet(weights='imagenet', include_top=False, input_shape=INPUT_SHAPE,)
    x=GlobalAveragePooling2D()(base_model.output)
    ses_predictions = Dense(1, activation='sigmoid',name="ses_output")(x)
    binomized_ses_pred = Concatenate(axis=-1)(Lambda(lambda x:[
        Multiply()([binom(NB_SES_CLASSES-1, k)*K.pow(x,k),K.pow(1-x,NB_SES_CLASSES-1-k)])
        for k in range(NB_SES_CLASSES)])(ses_predictions))

    # this is the model we will train
    model = Model(inputs=base_model.input,outputs=binomized_ses_pred)
    model.compile(optimizer=Adam(lr=INITIAL_LR), loss="categorical_crossentropy",
                  metrics=[fmeasure,recall,precision])

    model_checkpoint = ModelCheckpoint(MODEL_OUTPUT_DIR + "fold_{}-lastbest-0.hdf5".format(fold_id), verbose=1, save_best_only=True)
    early_stopping = EarlyStopping(patience=PATIENCE_BEFORE_STOPPING, restore_best_weights=True)
    tensorboard = TensorBoard(log_dir=MODEL_OUTPUT_DIR+"logs/fold_{}-{}".format(fold_id,time()),
                              histogram_freq=0, write_graph=False, write_images=False,
                              update_freq = 10)
    reduce_lr = ReduceLROnPlateau('loss', factor=LR_DECAY, patience=PATIENCE_BEFORE_LOWERING_LR, min_lr=1e-8)
    csv_logger = CSVLogger(MODEL_OUTPUT_DIR + "fold_{}-training_metrics.csv".format(fold_id))

    print("Training Model")
    global_epoch = 0
    restarts = 0
    last_best_losses = []
    last_best_epochs = []
    while global_epoch < MAX_EPOCH:
        history = model.fit_generator(
            generator=train_generator,
            steps_per_epoch = NB_SAMPLES_EPOCHS,#2000#len(train_generator), #train_image_count // BATCH_SIZE,
            epochs=MAX_EPOCH - global_epoch,
            validation_data=val_generator,
            validation_steps = 100,#1000,#len(val_generator), #val_image_count // BATCH_SIZE,
            workers=10,
            verbose=1,
            callbacks=[tensorboard, model_checkpoint, early_stopping, reduce_lr, csv_logger],
            shuffle=True
        )
        del history.model
        pickle.dump(history,open(MODEL_OUTPUT_DIR + "fold_{}-lastbest_history-{}.p".format(fold_id,restarts),"wb"))
        last_best_losses.append(min(history.history['val_loss']))
        last_best_local_epoch = history.history['val_loss'].index(min(history.history['val_loss']))
        last_best_epochs.append(global_epoch + last_best_local_epoch)
        if early_stopping.stopped_epoch == 0:
            print("Completed training after {} epochs.".format(MAX_EPOCH))
            break
        else:
            global_epoch = global_epoch + early_stopping.stopped_epoch - PATIENCE_BEFORE_STOPPING + 1
            print("Early stopping triggered after local epoch {} (global epoch {}).".format(
                early_stopping.stopped_epoch, global_epoch))
            print("Restarting from last best val_loss at local epoch {} (global epoch {}).".format(
                early_stopping.stopped_epoch - PATIENCE_BEFORE_STOPPING, global_epoch - PATIENCE_BEFORE_STOPPING))
            restarts = restarts + 1
            model.compile(optimizer=Adam(lr=INITIAL_LR/ 2 ** restarts),
                          loss="categorical_crossentropy",metrics=[fmeasure,recall,precision])
            model_checkpoint = ModelCheckpoint(MODEL_OUTPUT_DIR + "fold_{}-lastbest-{}.hdf5".format(fold_id,restarts),
                                               monitor='val_loss', verbose=1, save_best_only=True, mode='min')

    print("Saving Model")
    # Save last best model info
    with open(MODEL_OUTPUT_DIR + "fold_{}-last_best_models.csv".format(fold_id), 'w', newline='') as file:
        writer = csv.writer(file, delimiter=',')
        writer.writerow(['Model file', 'Global epoch', 'Validation loss'])
        for i in range(restarts + 1):
            writer.writerow(["fold_{}-lastbest-{}.hdf5".format(fold_id,i), last_best_epochs[i], last_best_losses[i]])

    # Load the last best model
    dic_load_model = {
        "precision":precision,
        "recall":recall,
        "fbeta_score":fbeta_score,
        "fmeasure":fmeasure,
        "binom":binom,
        "Multiply":Multiply,
        "Concatenate":Concatenate,
        "Lambda":Lambda,
        "NB_SES_CLASSES":NB_SES_CLASSES,
    }
    model = load_model(
        MODEL_OUTPUT_DIR + "fold_{}-lastbest-{}.hdf5".format(fold_id,last_best_losses.index(min(last_best_losses))),
        custom_objects=dic_load_model)

    print("Testing Model")
    # Evaluate model on test subset for kth fold
    ses_predictions = model.predict_generator(test_generator,test_image_count, workers=10, verbose=1)
    y_true_ses = test_generator.classes
    y_pred_ses = np.argmax(ses_predictions, axis=1)

    # Generate and print classification metrics and confusion matrix
    print("SES")
    print(classification_report(y_true_ses, y_pred_ses))
    ses_report = classification_report(y_true_ses, y_pred_ses, output_dict=True)

    with open(MODEL_OUTPUT_DIR + 'fold_{}-ses_classification_report.csv'.format(fold_id), 'w') as f:
        for key in ses_report.keys():
            f.write("%s,%s\n" % (key, ses_report[key]))
    ses_conf_arr = confusion_matrix(y_true_ses, y_pred_ses)
    print(ses_conf_arr)
    np.savetxt(MODEL_OUTPUT_DIR + "fold_{}-ses_confusion_matrix.csv".format(fold_id), ses_conf_arr, delimiter=",")
    
    # Clear model from GPU after each iteration
    K.clear_session()
