'''Utils faciliating direction io on precomputed format(aka cloudvolume).'''
import cloudvolume
from cloudvolume.lib import Bbox
from ffn.utils import bounding_box
import numpy as np
import logging
from pprint import pprint
import tensorflow as tf
from em_mask.io_utils import load_from_numpylike, preprocess_image
from tqdm import tqdm
from mpi4py import MPI
import os
mpi_comm = MPI.COMM_WORLD
mpi_rank = mpi_comm.Get_rank()
mpi_size = mpi_comm.Get_size()

def get_bboxes(union_bbox, chunk_size, overlap=(0,0,0), back_shift_small=False, backend='cloudvolume'):
  '''Use ffn subbox calculator to generate sequential overlapping bboxes'''
  if isinstance(union_bbox, Bbox):
    ffn_style_bbox = bounding_box.BoundingBox(
      np.array(union_bbox.minpt), np.array(union_bbox.size3()))
  else:
    ffn_style_bbox = union_bbox

  calc = bounding_box.OrderlyOverlappingCalculator(
    outer_box=ffn_style_bbox, 
    sub_box_size=chunk_size, 
    overlap=overlap, 
    include_small_sub_boxes=True,
    back_shift_small_sub_boxes=back_shift_small)
  bbs = list(calc.generate_sub_boxes())
  if backend == 'ffn':
    pass
  elif backend == 'cloudvolume':
    bbs = [Bbox(a=bb.start, b=bb.start+bb.size) for bb in bbs]
  else:
    raise ValueError('Use either ffn or cloudvolume')
  return bbs

def load_from_precomputed(coord_tensor, volume, chunk_shape, volume_axes='xyz'):
  """ Load a chunk from precomputed volume with border check.

  Args:
    coord: Tensor of shape (3,) in xyz order
    volume: A numpy like volume
    chunk_shape: 3-tuple/list of shape in xyz
    volume_axes: specify the axis order in volume
  Returns:
    Tensor loaded with data from coord
  """
  chunk_shape = np.array(chunk_shape)
  def _load_from_numpylike(coord):
    starts = np.array(coord[0]) - chunk_shape // 2
    bbox = Bbox(a=starts, b=starts+chunk_shape)
    data = volume[bbox][...]

    if volume_axes == 'xyz':
      data = data.transpose([2,1,0,3])
    elif volume_axes == 'zyx':
      pass
    else:
      raise ValueError('volume_axes mush either be "zyx" or "xyz"')
    return data

  dtype = volume.dtype
  num_classes = volume.shape[-1]
  logging.warn('weird class: %d %s', num_classes, volume.shape)
  with tf.name_scope('load_from_h5') as scope:
    loaded = tf.compat.v1.py_func(
        _load_from_numpylike, [coord_tensor], [dtype],
        name=scope)[0]
    loaded.set_shape(list(chunk_shape[::-1]) + [num_classes])
    logging.warn('after %s', loaded.shape)
    return loaded

def get_offset_and_size(cv_path):
  cv = cloudvolume.CloudVolume('file://%s' % cv_path, mip=0)
  input_offset = np.array(cv.info['scales'][0]['voxel_offset'])
  input_size = np.array(cv.info['scales'][0]['size'])
  return input_offset, input_size

def get_num_bbox(input_offset, input_size, chunk_size, overlap):
  union_bbox = Bbox(input_offset, input_offset + input_size)
  sub_bboxes = get_bboxes(union_bbox, chunk_size=chunk_size, overlap=overlap)
  return len(sub_bboxes)

def predict_input_fn_precomputed(
  input_volume,
  input_offset,
  input_size,
  input_mip,
  chunk_shape,
  label_shape,
  overlap,
  batch_size,
  offset,
  scale,
  var_threshold):
  
  cv_args = dict(
      bounded=False, fill_missing=True, autocrop=False,
      cache=False, compress_cache=None, cdn_cache=False,
      progress=False, provenance=None, compress=False, 
      non_aligned_writes=True, parallel=False)
  cv = cloudvolume.CloudVolume('file://%s' % input_volume, mip=input_mip, **cv_args)

  union_bbox = Bbox(input_offset, input_offset + input_size)
  sub_bboxes = get_bboxes(union_bbox, chunk_size=chunk_shape, overlap=overlap)
  
  rank_sub_bboxes = np.array_split(sub_bboxes, mpi_size)[mpi_rank]
  logging.warning('ranked %d bb %d', mpi_rank, len(rank_sub_bboxes))
  def dummy_gen():
    for sb in rank_sub_bboxes:
      yield [(sb.minpt + sb.maxpt) // 2]


  ds = tf.data.Dataset.from_generator(
    generator=dummy_gen, 
    output_types=(tf.int64), 
    output_shapes=(tf.TensorShape((1,3)))
  )
  ds = ds.map(lambda coord: (
      coord, 
      load_from_precomputed(coord, cv, chunk_shape, volume_axes='xyz')),
     num_parallel_calls=tf.data.experimental.AUTOTUNE)
  ds = ds.map(lambda coord, image: (coord, preprocess_image(image, offset, scale)),
     num_parallel_calls=tf.data.experimental.AUTOTUNE)
  ds = ds.map(lambda coord, image:
    {
      'center': coord,
      'image': image
    },
    num_parallel_calls=tf.data.experimental.AUTOTUNE)
  ds = ds.batch(batch_size)
  return ds

def writer(
  prediction_generator,
  output_volume,
  output_offset,
  output_size,
  chunk_shape,
  label_shape,
  resolution,
  overlap,
  num_iter
  ):
  if mpi_rank == 0:
    # create two separate cvs for output
    cv_args = dict(
        bounded=False, fill_missing=True, autocrop=False,
        cache=False, compress_cache=None, cdn_cache=False,
        progress=False, provenance=None, compress=False, 
        non_aligned_writes=True, parallel=False)
    logits_info = cloudvolume.CloudVolume.create_new_info(
      num_channels=1,
      layer_type='image',
      data_type='uint8',
      encoding='raw',
      resolution=resolution,
      voxel_offset=output_offset,
      volume_size=output_size,
      chunk_size=(256, 256, 64),
      max_mip=0,
      factor=(2,2,1))
    logits_path = os.path.join(output_volume, 'logits')
    logits_cv = cloudvolume.CloudVolume('file://%s' % logits_path, mip=0, info=logits_info, **cv_args)
    logits_cv.commit_info()
    class_info = cloudvolume.CloudVolume.create_new_info(
      num_channels=1,
      layer_type='segmentation',
      data_type='uint8',
      encoding='raw',
      resolution=resolution,
      voxel_offset=output_offset,
      volume_size=output_size,
      chunk_size=(256, 256, 64),
      max_mip=0,
      factor=(2,2,1))
    class_path = os.path.join(output_volume, 'class_predictions')
    class_cv = cloudvolume.CloudVolume('file://%s' % class_path, mip=0, info=class_info, **cv_args)
    class_cv.commit_info()
  else:
    logits_cv = None
    class_cv = None
  logits_cv = mpi_comm.bcast(logits_cv, 0)
  class_cv = mpi_comm.bcast(class_cv, 0)

  chunk_shape = np.array(chunk_shape)
  
  # write without padding 
  padding = np.array(overlap) // 2

  for i, p in tqdm(enumerate(prediction_generator), desc='bbox', total=num_iter): 
    assert 'logits' in p and 'class_prediction' in p


    bboxes = [
      Bbox(a=c[0] - chunk_shape // 2 + padding, 
           b=c[0] - chunk_shape // 2 + chunk_shape - padding)
      for c in p['center']
    ]

    for i, b in tqdm(enumerate(bboxes), desc='batch', disable=True):
      logits_chunk = p['logits'][i].transpose((2,1,0,3))
      class_chunk = p['class_prediction'][i].transpose((2,1,0))
      in_shape = logits_chunk.shape
      in_slc = np.s_[
        padding[0]:in_shape[0]-padding[0],
        padding[1]:in_shape[1]-padding[1],
        padding[2]:in_shape[2]-padding[2]
      ]
      norm_logits_chunk = np.floor(255 * (np.clip(logits_chunk, -0.5, 0.5) + 0.5)).astype(np.uint8)
      logits_cv[b] = norm_logits_chunk[in_slc]
      class_cv[b] = np.uint8(class_chunk[in_slc])
