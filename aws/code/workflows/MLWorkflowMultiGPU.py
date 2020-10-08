#
# Copyright (c) 2019-2020, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import time

import dask, dask_cudf
from dask_cuda import LocalCUDACluster
from dask.distributed import wait, Client

import cupy
import xgboost
import joblib

from dask_ml.model_selection import train_test_split
from cuml.dask.common.utils import persist_across_workers
from cuml.dask.ensemble import RandomForestClassifier
from cuml.metrics import accuracy_score

import warnings; warnings.filterwarnings("ignore")

from MLWorkflow import MLWorkflow, timer_decorator

class MLWorkflowMultiGPU ( MLWorkflow ):
    """ Multi-GPU Workflow """

    def __init__(self, hpo_config ):
        print( 'Multi-GPU Workflow')
        self.start_time = time.time()
        
        self.hpo_config = hpo_config
        self.dataset_cache = None
        
        self.cv_fold_scores = []
        self.best_score = -1

        self.cluster, self.client = self.cluster_initialize()
    
    @timer_decorator
    def cluster_initialize ( self ):
        """ Initialize the dask compute cluster based on the number of available GPU workers.
            XGBoost has a known issue where training fails if any worker has no data partition
            so when initializing a dask cluster for xgboost we may need to limit the number of workers
            see 3rd limitations bullet @ https://xgboost.readthedocs.io/en/latest/tutorials/dask.html 
        """

        cluster = None;  client = None
                
        self.n_workers = cupy.cuda.runtime.getDeviceCount()

        if 'XGBoost' in self.hpo_config.model_type:
            self.n_workers = min( len( self.hpo_config.target_files ), self.n_workers ) 

        cluster = LocalCUDACluster( n_workers = self.n_workers )
        client = Client( cluster )

        print(f'dask multi-GPU cluster with {self.n_workers} workers ')
        
        dask.config.set({'logging': {'loggers' : {'distributed.nanny': {'level': 'CRITICAL'}}}})
        dask.config.set({'temporary_directory' : self.hpo_config.output_artifacts_directory})

        return cluster, client

    @timer_decorator
    def ingest_data ( self ): 
        """ Ingest dataset, CSV and Parquet supported """

        if self.dataset_cache is not None:
            print( '> skipping ingestion, using cache')
            return self.dataset_cache

        if 'Parquet' in self.hpo_config.input_file_type:
            print('> parquet data ingestion')

            dataset = dask_cudf.read_parquet( self.hpo_config.target_files,
                                              columns = self.hpo_config.dataset_columns )

        elif 'CSV' in self.hpo_config.input_file_type:
            print('> csv data ingestion')
            # dtype = self.hpo_config.dataset_dtype, 

            dataset = dask_cudf.read_csv( self.hpo_config.target_files,
                                          names = self.hpo_config.dataset_columns,                                          
                                          header = 0 )
        
        print( f'\t dataset len: {len(dataset)}' )
        self.dataset_cache = dataset
        return dataset

    @timer_decorator
    def handle_missing_data ( self, dataset ): 
        """ Drop samples with missing data [ inplace i.e., do not copy dataset ] """
        dataset = dataset.dropna()
        return dataset

    @timer_decorator
    def split_dataset ( self, dataset, random_state ): 
        """ 
        Split into train and test data subsets, using CV-fold index for randomness 
            Note: Since Dask has a lazy execution model, so far we've built up a computation graph, 
                  however, no computation has occured. By using a persist_accross_workers call we force execution.
                  This is helpful prior to model training
        """
        print('> train-test split')
        label_column = self.hpo_config.label_column
        
        train, test = train_test_split( dataset, random_state = random_state ) 

        # build X [ features ], y [ labels ] for the train and test subsets
        y_train = train[label_column]; 
        X_train = train.drop(label_column, axis = 1)
        y_test = test[label_column]
        X_test = test.drop(label_column, axis = 1)

        # force execution
        X_train, y_train, X_test, y_test = persist_across_workers( self.client,
                                                                    [ X_train, y_train, X_test, y_test ], 
                                                                    workers = self.client.has_what().keys() )        

        return X_train.astype( self.hpo_config.dataset_dtype ),\
               X_test.astype( self.hpo_config.dataset_dtype ),\
               y_train.astype( self.hpo_config.dataset_dtype ),\
               y_test.astype( self.hpo_config.dataset_dtype ) 

    @timer_decorator
    def fit ( self, X_train, y_train ):       
        """ Fit decision tree model [ architecture defined by HPO parameters ] """
        if 'XGBoost' in self.hpo_config.model_type:
            print('> fit xgboost model')
            dtrain = xgboost.dask.DaskDMatrix( self.client, X_train, y_train)
            xgboost_output = xgboost.dask.train( self.client, self.hpo_config.model_params, dtrain, 
                                                num_boost_round = self.hpo_config.model_params['num_boost_round'] )
            trained_model = xgboost_output['booster']

        elif 'RandomForest' in self.hpo_config.model_type:
            print('> fit randomforest model')
            trained_model = RandomForestClassifier ( n_estimators = self.hpo_config.model_params['n_estimators'],
                                                     max_depth = self.hpo_config.model_params['max_depth'],
                                                     max_features = self.hpo_config.model_params['max_features'],
                                                     n_bins = self.hpo_config.model_params['n_bins'] )\
                                                     .fit( X_train, y_train.astype('int32') )                                                     
        return trained_model 
    
    @timer_decorator
    def predict ( self, trained_model, X_test, threshold = 0.5 ):
        """ Inference with the trained model on the unseen test data """

        print('> predict with trained model ')
        if 'XGBoost' in self.hpo_config.model_type:
            dtest = xgboost.dask.DaskDMatrix( self.client, X_test )
            predictions = xgboost.dask.predict( self.client, trained_model, dtest).compute() 
            predictions = (predictions > threshold ) * 1.0                    
            
        elif 'RandomForest' in self.hpo_config.model_type:
            predictions = trained_model.predict( X_test ).compute()

        return predictions

    @timer_decorator
    def score ( self, y_test, predictions ): 
        """ Score predictions vs ground truth labels on test data """
        print('> score predictions')
        y_test = y_test.compute()
        score = accuracy_score ( y_test.astype( self.hpo_config.dataset_dtype ),
                                predictions.astype( self.hpo_config.dataset_dtype ) )

        print(f'\t score = {score}')
        self.cv_fold_scores += [score]
        return score

    def save_best_model ( self, score, trained_model, filename = 'saved_model' ): 
        """  Persist/save model that sets a new high score """

        if score > self.best_score:
            self.best_score = score
            print('> saving high-scoring model')
            output_filename = self.hpo_config.model_store_directory + '/' + str( filename )
            if 'XGBoost' in self.hpo_config.model_type:
                trained_model.save_model( output_filename + '_mgpu_xgb' )
            elif 'RandomForest' in self.hpo_config.model_type:
                joblib.dump ( trained_model, output_filename + '_mgpu_rf' )
            
    @timer_decorator
    async def cleanup ( self, i_fold ):
        """ Close and restart the cluster when multiple cross validation folds are used to prevent memory creep. """    
        if i_fold == self.hpo_config.cv_folds -1:
            print('> done all folds; closing cluster\n')
            await self.client.close()
            await self.cluster.close()
        elif i_fold < self.hpo_config.cv_folds - 1:            
            print('> end of fold; reinitializing cluster\n')
            await self.client.close()
            await self.cluster.close()
            self.cluster, self.client = self.cluster_initialize()

    def emit_final_score ( self ):
        """ Emit score for parsing by the cloud HPO orchestrator """

        print(f'total_time = {round( time.time() - self.start_time )} seconds ')

        if self.hpo_config.cv_folds > 1 :
            print(f'fold scores : {self.cv_fold_scores} \n')

        final_score = sum(self.cv_fold_scores) / len(self.cv_fold_scores) # average

        print(f'final-score: {final_score}; \n')