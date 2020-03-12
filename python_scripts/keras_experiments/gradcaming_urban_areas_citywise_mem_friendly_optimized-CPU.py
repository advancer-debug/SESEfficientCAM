from rasterstats import zonal_stats, point_query, gen_zonal_stats
import keras
import rasterio
import rtree
from keras import backend as K
from keras.optimizers import SGD, Adam
import pandas as pd
import cv2
import geopandas as gpd
import os,sys
from tqdm import tqdm as tqdmn
import numpy as np
from keras.callbacks import ModelCheckpoint, EarlyStopping, ReduceLROnPlateau, TensorBoard, CSVLogger
from keras import metrics
from keras.models import Model, load_model
from sklearn.metrics import confusion_matrix, classification_report
from keras.layers import Input
from skimage import io
from matplotlib.cm import inferno
import glob
from rasterio.transform import from_bounds
from efficientnet import EfficientNetB0 as EfficientNet
sys.path.append("/warehouse/COMPLEXNET/jlevyabi/SATELSES/equirect_proj_test/cnes/python_files/satellite/")
from generate_fr_ua_vhr_data import generate_car_census_data
sys.path.append("/warehouse/COMPLEXNET/jlevyabi/SATELSES/equirect_proj_test/cnes/python_files/aerial/")
from aerial_training_utils import generate_full_idINSPIRE, geographical_boundaries, my_preprocessor, fmeasure,recall,precision, fbeta_score
import tensorflow as tf
from keras.preprocessing import image
from keras.layers.core import Lambda
from keras.models import Sequential
from tensorflow.python.framework import ops
from functools import reduce
from scipy.stats import entropy
import pickle
from keras import backend as K
import argparse
from keras.layers import Dense, Flatten, Dropout, GlobalAveragePooling2D, Concatenate, Input, Lambda, Multiply
from scipy.special import binom
from sklearn.model_selection import StratifiedKFold
import multiprocessing
from joblib import Parallel, delayed
from time import time
import gc
import socket

# Global paths
BASE_DIR = "/warehouse/COMPLEXNET/jlevyabi/"
SAT_DIR = BASE_DIR + "SATELSES/equirect_proj_test/cnes/data_files/esa/URBAN_ATLAS/"
CENSUS_DIR = BASE_DIR + 'REPLICATE_LINGSES/data_files/census_data/'
UA_DIR = BASE_DIR + "SATELSES/equirect_proj_test/cnes/data_files/land_ua_esa/FR/"
OUTPUT_DIR = BASE_DIR + "SATELSES/equirect_proj_test/cnes/data_files/outputs/AERIAL_esa_URBAN_ATLAS_FR/"
MODEL_OUTPUT_DIR = BASE_DIR + "SATELSES/equirect_proj_test/cnes/data_files/outputs/model_data/efficientnet_keras/"

# Global variables

# Image Related
W = H = 800
IMG_SIZE = (W, H)
INPUT_SHAPE = (IMG_SIZE[0], IMG_SIZE[1], 3)

# SES/UA Related
NB_SES_CLASSES = 5
ADRIAN_ALBERT_THRESHOLD = .25
INSEE_AREA = 200*200

# Model Related
conv_name = 'conv2d_65'
input_name = 'input_1'
machine = socket.gethostname()
# Argument Parsing
print("Parsing Arguments...")
parser = argparse.ArgumentParser()
parser.add_argument('-city','--city',help = 'City to study')
parser.add_argument('-model_dir','--model_dir', help = 'Model Weight Storage Location', type=str,
                    default="2019_income_norm_v2/")
parser.add_argument('-max_bs','--max_bs',help = 'Batch Size', type=int, default=20)
parser.add_argument('-workload','--workload', type=int, default=1000)
parser.add_argument('-start','--start',help = 'start', type=int, default=0)
parser.add_argument('-end','--end',help = 'end', type=int, default=-1)

args = parser.parse_args()
MODEL_OUTPUT_DIR = MODEL_OUTPUT_DIR + args.model_dir
WORKLOAD = args.workload #(Outer Batch Size)
MAX_BS = args.max_bs #(Inner Batch Size )
start = args.start 
end = args.end 

if not MODEL_OUTPUT_DIR.endswith("/"):
    MODEL_OUTPUT_DIR+="/"
city = args.city
assert city in os.listdir(MODEL_OUTPUT_DIR), "Model not trained for city: %s" %city

cfg = K.tf.ConfigProto()
#      intra_op_parallelism_threads=1,
#      inter_op_parallelism_threads=1)
K.set_session(K.tf.Session(config=cfg))
#K.set_session(K.tf.Session(config=cfg))

#Base class for saliency masks. Alone, this class doesn't do anything.
class SaliencyMask(object):
    """Base class for saliency masks. Alone, this class doesn't do anything."""
    def __init__(self, model, output_index=0):
        """Constructs a SaliencyMask.
        Args:
            model: the keras model used to make prediction
            output_index: the index of the node in the last layer to take derivative on
        """
        pass
    #
    def get_mask(self, input_image):
        """Returns an unsmoothed mask.
        Args:
            input_image: input image with shape (H, W, 3).
        """
        pass
    #
    def get_smoothed_mask(self, input_image, stdev_spread=1, nsamples=100):
        """Returns a mask that is smoothed with the SmoothGrad method.
        Args:
            input_image: input image with shape (H, W, 3).
        """
        stdev = stdev_spread * (np.max(input_image) - np.min(input_image))
        total_gradients = np.zeros_like(input_image)
        for i in range(nsamples):
            noise = np.random.normal(0, stdev, input_image.shape)
            x_value_plus_noise = input_image + noise
            total_gradients += self.get_mask(x_value_plus_noise)
        return total_gradients / nsamples

#A SaliencyMask class that computes saliency masks with a gradient.
class GradientSaliency(SaliencyMask):
    """A SaliencyMask class that computes saliency masks with a gradient."""
    def __init__(self, model, output_index=0):
        # Define the function to compute the gradient
        input_tensors = [model.input,        # placeholder for input image tensor
                         K.learning_phase(), # placeholder for mode (train or test) tense
                        ]
        gradients = model.optimizer.get_gradients(model.output[0][output_index], model.input)
        self.compute_gradients = K.function(inputs=input_tensors, outputs=gradients)
    #
    def get_mask(self, input_image):
        """Returns a vanilla gradient mask.
        Args:
            input_image: input image with shape (H, W, 3).
        """
        # Execute the function to compute the gradient
        x_value = np.expand_dims(input_image, axis=0)
        gradients = self.compute_gradients([x_value, 0])[0][0]
        return gradients

#A SaliencyMask class that computes saliency masks with GuidedBackProp.
class GuidedBackprop(SaliencyMask):
    """A SaliencyMask class that computes saliency masks with GuidedBackProp.
    This implementation copies the TensorFlow graph to a new graph with the ReLU
    gradient overwritten as in the paper: https://arxiv.org/abs/1412.6806
    """
    GuidedReluRegistered = False
    def __init__(self, model, output_index=0, custom_loss=None):
        #
        model_save_loc = '/tmp/gbbis_keras_cpu_{}_{}_{}.h5'.format(city,output_index,machine)
        session_save_loc = '/tmp/guided_backpropbis_ckpt_cpu_{}_{}_{}'.format(city,output_index,machine)
        graph_save_loc = '/tmp/guided_backpropbis_ckpt_cpu_{}_{}_{}.meta'.format(city,output_index,machine)
        #
        """Constructs a GuidedBackprop SaliencyMask."""
        if GuidedBackprop.GuidedReluRegistered is False:
            @tf.RegisterGradient("GuidedRelu")
            def _GuidedReluGrad(op, grad):
                gate_g = tf.cast(grad > 0, "float32")
                gate_y = tf.cast(op.outputs[0] > 0, "float32")
                return gate_y * gate_g * grad
        GuidedBackprop.GuidedReluRegistered = True
        """ 
            Create a dummy session to set the learning phase to 0 (test mode in keras) without 
            inteferring with the session in the original keras model. This is a workaround
            for the problem that tf.gradients returns error with keras models that contains 
            Dropout or BatchNormalization.

            Basic Idea: save keras model => create new keras model with learning phase set to 0 => save
            the tensorflow graph => create new tensorflow graph with ReLU replaced by GuidedReLU.
        """   
        model.save(model_save_loc) 
        with tf.Graph().as_default(): 
            with tf.Session(config=cfg).as_default(): 
                K.set_learning_phase(0)
                load_model(model_save_loc,
                           custom_objects={"custom_loss":custom_loss,"precision":precision,
                                           "recall":recall,"fbeta_score":fbeta_score,"fmeasure":fmeasure,
                                           "binom":binom,"Multiply":Multiply,"Concatenate":Concatenate,
                                           "Lambda":Lambda,"NB_SES_CLASSES":NB_SES_CLASSES})
                session = K.get_session()
                tf.train.export_meta_graph()
                saver = tf.train.Saver()
                saver.save(session,session_save_loc)
        self.guided_graph = tf.Graph()
        with self.guided_graph.as_default():
            self.guided_sess = tf.Session(graph = self.guided_graph,config=cfg)
            with self.guided_graph.gradient_override_map({'Relu': 'GuidedRelu'}):
                saver = tf.train.import_meta_graph(graph_save_loc)
                saver.restore(self.guided_sess, session_save_loc)
                self.imported_y = self.guided_graph.get_tensor_by_name(model.output.name)[0][output_index]
                self.imported_x = self.guided_graph.get_tensor_by_name(model.input.name)
                self.guided_grads_node = tf.gradients(self.imported_y, self.imported_x)
        gc.collect()

    #
    def get_mask(self, input_image):
        """Returns a GuidedBackprop mask."""
        x_value = np.expand_dims(input_image, axis=0)
        guided_feed_dict = {}
        guided_feed_dict[self.imported_x] = x_value
        gradients = self.guided_sess.run(self.guided_grads_node, feed_dict = guided_feed_dict)[0][0]
        return gradients
    #
    def get_mult_mask(self, input_images):
        """Returns a GuidedBackprop mask."""
        guided_feed_dict = {}
        guided_feed_dict[self.imported_x] = input_images
        gradients = self.guided_sess.run(self.guided_grads_node, feed_dict = guided_feed_dict)[0][0]
        return gradients

# Yields Intersection of Area between polygons
def find_intersects(a1, a2):
    if  a1.intersects(a2):
        return (a1.intersection(a2)).area
    else:
        return 0

# Spatial Join for assigning cells to SES
def sjoin(left_df, right_df, how='inner', op='intersects', lsuffix='left', rsuffix='right'):
    index_left = 'index_%s' % lsuffix
    index_right = 'index_%s' % rsuffix
    if (any(left_df.columns.isin([index_left, index_right]))
            or any(right_df.columns.isin([index_left, index_right]))):
        raise ValueError("'{0}' and '{1}' cannot be names in the frames being"
                         " joined".format(index_left, index_right))
    #
    left_df = left_df.copy(deep=True)
    left_df.index = left_df.index.rename(index_left)
    left_df = left_df.reset_index()
    right_df = right_df.copy(deep=True)
    right_df.index = right_df.index.rename(index_right)
    right_df = right_df.reset_index()
    # insert the bounds in the rtree spatial index
    right_df_bounds = right_df.geometry.apply(lambda x: x.bounds)
    stream = ((i, b, None) for i, b in (enumerate(right_df_bounds)))
    tree_idx = rtree.index.Index(stream)
    idxmatch = (left_df.geometry.apply(lambda x: x.bounds)
                .apply(lambda x: list(tree_idx.intersection(x))))
    #
    one_to_many_idxmatch = idxmatch[idxmatch.apply(len) > 0]
    if one_to_many_idxmatch.shape[0] > 0:
        r_idx = np.concatenate(one_to_many_idxmatch.values)
        l_idx = np.concatenate([[i] * len(v) for i, v in one_to_many_idxmatch.iteritems()])
        check_predicates = np.vectorize(find_intersects)
        result_one_to_many = (pd.DataFrame(np.column_stack([l_idx, r_idx,
                                                            check_predicates(left_df.geometry[l_idx],
                                                            right_df[right_df.geometry.name][r_idx])])))
        result_one_to_many.columns = ['_key_left', '_key_right', 'match_bool']
        result_one_to_many._key_left = result_one_to_many._key_left.astype(int)
        result_one_to_many._key_right = result_one_to_many._key_right.astype(int)
        result_one_to_many = pd.DataFrame(result_one_to_many[result_one_to_many['match_bool'] > 0])
        result_one_to_many = result_one_to_many.groupby("_key_right").apply(lambda x : list(x["_key_left"]))
    return result_one_to_many

#GradCAM method for visualizing input saliency for processing multiple images in one run. (MAX_BS)
def grad_cam_batch(input_model, images, classes, layer_name):
    """GradCAM method for visualizing input saliency.
    Same as grad_cam but processes multiple images in one run."""
    loss = tf.gather_nd(input_model.output, np.dstack([range(images.shape[0]), classes])[0])
    layer_output = input_model.get_layer(layer_name).output
    grads = K.gradients(loss, layer_output)[0]
    gradient_fn = K.function([input_model.input, K.learning_phase()], [layer_output, grads])
    #
    conv_output, grads_val = gradient_fn([images, 0])    
    weights = np.mean(grads_val, axis=(1, 2))
    cams = np.einsum('ijkl,il->ijk', conv_output, weights)
    #
    # Process CAMs
    new_cams = np.empty((images.shape[0], H, W))
    new_cams_rz = np.empty((images.shape[0], H, W))
    for i in range(new_cams.shape[0]):
        cam_i = cams[i] - cams[i].mean()
        cam_i = (cam_i + 1e-10) / (np.linalg.norm(cam_i, 2) + 1e-10)
        #cam_i = cams[i]
        new_cams[i] = cv2.resize(cam_i, (W, H), cv2.INTER_LINEAR)
        new_cams[i] = np.maximum(new_cams[i], 0)
        new_cams_rz[i] = new_cams[i] / new_cams[i].max()  
    del loss, layer_output, grads, gradient_fn, conv_output, grads_val
    return new_cams, new_cams_rz

# Calculates Raster Statistics
def individual_rastering(gradcam_bg,t,test_cores,class_,time_id):
    raster_loc = BASE_DIR + 'tmp/new_testbis_cpu_{}_{}_{}_{}.tif'.format(city,time_id,class_,machine)
    poly_loc = BASE_DIR + 'tmp/new_poly2bis_cpu_{}_{}_{}.shp'.format(city,time_id,machine)
    class_dataset = rasterio.open(raster_loc,'w',driver='GTiff',
                                  height=H, width=W,count=1,dtype=gradcam_bg.dtype,crs={'init': 'epsg:3035'},transform=t)
    class_dataset.write(gradcam_bg, 1)
    class_dataset.close()
    my_ops = ['sum','min','max','median','mean','count']
    test_cores[["ITEM2012","geometry"]].to_file(poly_loc)
    stats_class = zonal_stats(poly_loc,raster_loc,stats=my_ops,geojson_out=True)
    return gpd.GeoDataFrame.from_features(stats_class).rename({k:str(class_)+ "_" + k for k in my_ops},axis=1)

# Calculates TV distance within polygons in raster
def individual_totvar(gradcam_gdf,class_poor,grad_cam_poor,class_rich,grad_cam_rich,time_id):
    raster_loc_poor = BASE_DIR + 'tmp/new_testbis_cpu_{}_{}_{}_{}.tif'.format(city,time_id,class_poor,machine)
    raster_loc_rich = BASE_DIR + 'tmp/new_testbis_cpu_{}_{}_{}_{}.tif'.format(city,time_id,class_rich,machine)
    poly_loc = BASE_DIR + 'tmp/new_poly2bis_cpu_{}_{}_{}.shp'.format(city,time_id,machine)
    if grad_cam_poor.sum() == 0 or grad_cam_rich.sum() == 0:
        tot_var_data = sym_KL = None
    else:
        tot_var_data, sym_KL = [], []
        stats_totvar_poor = zonal_stats(poly_loc,raster_loc_poor,stats='sum',raster_out=True,)
        stats_totvar_rich = zonal_stats(poly_loc,raster_loc_rich,stats='sum',raster_out=True,)
        for k in list(gradcam_gdf.index):
            P = stats_totvar_poor[k]["mini_raster_array"].data
            P /= np.sum(P[stats_totvar_poor[k]["mini_raster_array"].mask])
            P = P[stats_totvar_poor[k]["mini_raster_array"].mask]
            #
            Q = stats_totvar_rich[k]["mini_raster_array"].data
            Q /= np.sum(Q[stats_totvar_rich[k]["mini_raster_array"].mask])
            Q = Q[stats_totvar_rich[k]["mini_raster_array"].mask]
            #
            tot_var_data.append(0.5*np.sum(np.abs(P-Q)))
            sym_KL.append(entropy(P,Q)+entropy(Q,P))
    return tot_var_data, sym_KL, raster_loc_poor, raster_loc_rich, poly_loc

# Calculates all raster statistics for one sample
def individual_statistics(gradcam_cam_poor,gradcam_bg_poor,class_poor,
                          gradcam_cam_rich,gradcam_bg_rich,class_rich,
                          t,test_core,val_idINSPIRE):
    id_core = str(time())
    gradcam_gdf_poor = individual_rastering(gradcam_bg_poor,t,test_core,class_poor,id_core)
    gradcam_gdf_rich = individual_rastering(gradcam_bg_rich,t,test_core,class_rich,id_core)
    gradcam_gdf = pd.concat([gradcam_gdf_poor,
                             gradcam_gdf_rich[['4_sum','4_min','4_max','4_median','4_mean','4_count']]],axis=1)
    tot_var_data, sym_KL, raster_loc_poor, raster_loc_rich, poly_loc = individual_totvar(gradcam_gdf,
                                                                                         class_poor,gradcam_cam_poor,
                                                                                         class_rich,gradcam_cam_rich,
                                                                                         id_core)
    os.system("rm {}".format(raster_loc_poor))
    os.system("rm {}".format(raster_loc_rich))
    os.system("rm {}".format(poly_loc))
    os.system("rm {}".format(poly_loc.replace(".shp",".shx")))
    os.system("rm {}".format(poly_loc.replace(".shp",".dbf")))
    os.system("rm {}".format(poly_loc.replace(".shp",".prj")))
    os.system("rm {}".format(poly_loc.replace(".shp",".cpg")))
    gradcam_gdf["totvar"] = tot_var_data
    gradcam_gdf["sym_KL"] = sym_KL
    gradcam_gdf["area"] = gradcam_gdf.geometry.area
    gradcam_gdf["poor_score"] = gradcam_gdf["0_sum"]/gradcam_gdf["area"]
    gradcam_gdf["rich_score"] = gradcam_gdf["4_sum"]/gradcam_gdf["area"]
    return (t,test_core,gradcam_gdf,val_idINSPIRE)


# Preprocess Image
def load_prepared_img(im_name):
    return cv2.resize(my_preprocessor(cv2.imread(im_name)),IMG_SIZE)    

# Runs through inner batch  for 1 outer batch iteration
def serialize_gradcaming(model,guided_bprop,sample_census_cell_imnames,indices,ind_batched_list,class_):
    # CAM
    full_grad_cams, full_grad_cam_bgs = [], []
    # Guided BackPropation  
    print("Batching")
    for i in tqdmn(range(len(ind_batched_list)-1)):
        batch_sample_census_cell_imnames = sample_census_cell_imnames[ind_batched_list[i]:ind_batched_list[i+1]]
        batch_sample_census_cell_imgs = [load_prepared_img(im) for im in batch_sample_census_cell_imnames]
        batch_classes = [class_ for j in range(ind_batched_list[i],ind_batched_list[i+1])]
        grad_cams, grad_cam_rzs = grad_cam_batch(model,np.stack(batch_sample_census_cell_imgs),
                                                         batch_classes, conv_name)
        masks = [guided_bprop.get_mask(img) for img in batch_sample_census_cell_imgs]
        images = np.stack([np.sum(np.abs(mask), axis=2) for mask in masks])
        # Combination
        gradcam_bgs = np.multiply(grad_cam_rzs,images)
        upper_percs = np.percentile(gradcam_bgs,99,(1,2))
        gradcam_bgs = np.minimum(gradcam_bgs,np.stack([k *np.ones((W,H)) for k in upper_percs]))
        full_grad_cams.append(grad_cams)
        full_grad_cam_bgs.append(gradcam_bgs)
    full_grad_cams = np.vstack(full_grad_cams) if len(full_grad_cams) > 1 else full_grad_cams
    full_grad_cam_bgs = np.vstack(full_grad_cam_bgs) if  len(full_grad_cam_bgs) > 1 else full_grad_cam_bgs
    return full_grad_cams,full_grad_cam_bgs

# Runs everything
def serialize_treating(ua_data,gdf_full_im_df_sampled,indices,ind_list,class_poor,class_rich):
    print("Overlaying")
    test_cores = [gpd.overlay(ua_data.iloc[indices[ind]], gdf_full_im_df_sampled.iloc[ind:(ind+1)], how='intersection')
                 for ind in tqdmn(ind_list)]
    print("Bounding")
    ts = [from_bounds(
        gdf_full_im_df_sampled[ind:(ind+1)].bounds.minx.values[0]+0,
        gdf_full_im_df_sampled[ind:(ind+1)].bounds.miny.values[0]+0,
        gdf_full_im_df_sampled[ind:(ind+1)].bounds.maxx.values[0]+0,
        gdf_full_im_df_sampled[ind:(ind+1)].bounds.maxy.values[0]+0,
        W, H) for ind in tqdmn(ind_list)]
    print("Generating Images")
    sample_cell_datas = [gdf_full_im_df_sampled.iloc[ind] for ind in tqdmn(ind_list) ]
    sample_census_cell_imnames = [OUTPUT_DIR + val.path2im for val in tqdmn(sample_cell_datas)]
    #indices to distribute among cores
    folds_data = pd.concat(
        [pd.read_csv(fold_file,header=0,sep=",")
         for fold_file in glob.glob(MODEL_OUTPUT_DIR+city+"/*last_best_models.csv")], axis=0).reset_index(drop=True)
    
    best_model_city = folds_data.ix[folds_data["Validation loss"].idxmin()]["Model file"]
    print("Loading Weights {}".format(best_model_city))
    #
    data = []
    # Load the last best model
    dic_load_model = {
        "precision":precision,"recall":recall,"fbeta_score":fbeta_score,"fmeasure":fmeasure,
        "binom":binom,"Multiply":Multiply,"Concatenate":Concatenate,"Lambda":Lambda,"NB_SES_CLASSES":NB_SES_CLASSES,
    }
    model = load_model(MODEL_OUTPUT_DIR+city+"/"+best_model_city,custom_objects=dic_load_model)
    model.compile(loss='categorical_crossentropy', optimizer='adam')
    guided_bprop_poor = GuidedBackprop(model,output_index=class_poor);
    guided_bprop_rich = GuidedBackprop(model,output_index=class_rich);
    #
    print("Grunt Work")
    BATCH_IDENTITIES = range(0,len(ind_list),WORKLOAD)
    for id_batch_idx,batch_idx in enumerate(BATCH_IDENTITIES):
        print("Batch {}/{}".format(1+id_batch_idx,len(BATCH_IDENTITIES)))
        batch_ind_list = ind_list[batch_idx:(batch_idx+WORKLOAD)]
        batch_sample_census_cell_imnames = sample_census_cell_imnames[batch_idx:(batch_idx+WORKLOAD)]
        batch_indices = indices[batch_idx:(batch_idx+WORKLOAD)]
        ind_batched_list = list(np.arange(0,len(batch_ind_list),MAX_BS))
        if ind_batched_list[-1] != len(batch_ind_list):
            ind_batched_list.append(len(batch_ind_list))
        #
        print("GradCaming the sparse")
        gradcam_cams_poor, gradcam_bgs_poor = serialize_gradcaming(model,guided_bprop_poor,
                                                                   batch_sample_census_cell_imnames,batch_indices,
                                                                   ind_batched_list,class_poor)
        print("GradCaming the dense")
        gradcam_cams_rich, gradcam_bgs_rich = serialize_gradcaming(model,guided_bprop_rich,
                                                                   batch_sample_census_cell_imnames,batch_indices,
                                                                   ind_batched_list,class_rich)      
        print("Computing raster statistics")
        n_jobs = 15
        pre_data = Parallel(n_jobs=n_jobs)(delayed(individual_statistics)
                                       (gradcam_cams_poor[j],gradcam_bgs_poor[j],class_poor,
                                        gradcam_cams_rich[j],gradcam_bgs_rich[j],class_rich,
                                        ts[ind],test_cores[ind],
                                        gdf_full_im_df_sampled.iloc[ind:(ind+1)].idINSPIRE.values[0])
                                       for j,ind in tqdmn(enumerate(batch_ind_list)))
        data.append(pre_data)
    return [val for pre_data in data for val in pre_data]

if __name__ == '__main__':
    print("GradCAMING {} with model defined in {} with {}".format(city,args.model_dir,machine))
    print("Generating Full DataSet")
    full_im_df = generate_full_idINSPIRE(UA_DIR, OUTPUT_DIR, NB_SES_CLASSES, ADRIAN_ALBERT_THRESHOLD, INSEE_AREA)
    city_assoc = pd.read_csv(OUTPUT_DIR + "city_assoc.csv")
    full_im_df_ua = pd.merge(full_im_df,city_assoc,on="idINSPIRE");
    full_im_df_ua = full_im_df_ua[full_im_df_ua.FUA_NAME == city].iloc[start:end]
    #
    gdf_full_im_df_sampled = full_im_df_ua.to_crs({'init': 'epsg:3035'})
    #
    print("Generating UA DataSet")
    ua_data = gpd.GeoDataFrame(pd.concat([gpd.read_file(d) 
                                          for d in tqdmn(glob.glob(UA_DIR+"**/Shapefiles/*UA2012.shp"))]))
    ua_data.crs = {'init': 'epsg:3035'}
    #
    print("Joining UA + Full")
    indices = sjoin(ua_data,gdf_full_im_df_sampled)
    #
    print("GradCaming Urban Environments")
    class_poor = 0
    class_rich = NB_SES_CLASSES - 1
    test = serialize_treating(ua_data,gdf_full_im_df_sampled,indices,
                              range(gdf_full_im_df_sampled.shape[0]),class_poor,class_rich)
    pickle.dump(test,
                open(MODEL_OUTPUT_DIR+city+"/preds/urbanization_patterns_cpu_{}_{}_{}-{}_income.p".format(
                    city,machine,start,end), "wb"))
