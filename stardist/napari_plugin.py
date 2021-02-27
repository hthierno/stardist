"""
TODO:
- cache selected models?
- option to use CPU or GPU, limit GPU memory ('allow_growth'?)
- execute in different thread, ability to cancel?
- normalize per channel or jointly
- other output types (labels, shapes, surface). possible to choose dynamically?
- support custom models from file system (also url?)
- advanced options (n_tiles, boundary exclusion, show cnn output)
- restore defaults button
- make ui pretty
- how to deal with errors? catch and show to user?
- let user choose output type (labels, polys, both)
- show progress for tiled prediction and/or timelapse processing?
"""

from napari_plugin_engine import napari_hook_implementation
from magicgui import magicgui
# from magicgui.widgets import ProgressBar

import numpy as np
from csbdeep.utils import _raise, normalize, axes_check_and_normalize
from csbdeep.models.pretrained import get_registered_models
from .models import StarDist2D, StarDist3D
from pathlib import Path

import napari
from napari.utils.colormaps import label_colormap
from typing import List
from enum import Enum

# TODO: inelegant (wouldn't work if a pretrained model is called the same as CUSTOM_MODEL)
CUSTOM_MODEL = 'Custom Model'

# get available models
_models2d, _aliases2d = get_registered_models(StarDist2D)
_models3d, _aliases3d = get_registered_models(StarDist3D)
# use first alias for model selection (if alias exists)
models2d = [(_aliases2d[m][0] if len(_aliases2d[m]) > 0 else m) for m in _models2d] + [CUSTOM_MODEL]
models3d = [(_aliases3d[m][0] if len(_aliases3d[m]) > 0 else m) for m in _models3d] + [CUSTOM_MODEL]

class Output(Enum):
    Labels = 'Label Image'
    Polys  = 'Polygons / Polyhedra'
    Both   = 'Both'
output_choices = [Output.Labels.value, Output.Polys.value, Output.Both.value]


DEFAULTS = dict (
    model2d      = models2d[0],
    model3d      = models3d[0],
    norm_image   = True,
    perc_low     =  1.0,
    perc_high    = 99.8,
    prob_thresh  = 0.5,
    nms_thresh   = 0.4,
    output_type  = Output.Both.value,
    n_tiles      = 'None',
    cnn_output   = False,
)


def _is3D(image):
    # TODO: possible to know if an image is multi-channel/timelapse 2D or single-channel 3D?
    # TODO: best would be to know image axes...
    return image.data.ndim == 3 and image.rgb == False


def surface_from_polys(polys):
    from stardist.geometry import dist_to_coord3D
    dist = polys['dist']
    points = polys['points']
    rays_vertices = polys['rays_vertices']
    rays_faces = polys['rays_faces'].copy()
    coord = dist_to_coord3D(dist, points, rays_vertices)

    if not all((coord.ndim==3, coord.shape[-1]==3, rays_faces.shape[-1]==3)):
        raise ValueError(f"Wrong shapes! coord -> (m,n,3) rays_faces -> (k,3)")

    vertices, faces, values = [], [], []
    for i, xs in enumerate(coord, start=1):
        # values.extend(np.random.uniform(0.3,1)+np.random.uniform(-.1,.1,len(xs)))
        values.extend(i+np.zeros(len(xs)))
        vertices.extend(xs)
        faces.extend(rays_faces.copy())
        rays_faces += len(xs)

    return [np.array(vertices), np.array(faces), np.array(values)]



def widget_wrapper():

    @magicgui (
        label_head      = dict(widget_type='Label', label='<h1>StarDist</h1>'),
        image           = dict(label='Input Image'),
        axes            = dict(widget_type='LineEdit', label='Input Axes'),
        label_nn        = dict(widget_type='Label', label='<br>Neural Network Prediction:'),
        model2d         = dict(widget_type='ComboBox', label='2D Model', choices=models2d),
        model3d         = dict(widget_type='ComboBox', label='3D Model', choices=models3d),
        model_folder    = dict(widget_type='FileEdit', label=' ', mode='d'),
        norm_image      = dict(widget_type='CheckBox', text='Normalize Image'),
        label_nms       = dict(widget_type='Label', label='<br>NMS Postprocessing:'),
        perc_low        = dict(widget_type='FloatSpinBox', label='Percentile low',              min=0.0, max=100.0, step=0.1),
        perc_high       = dict(widget_type='FloatSpinBox', label='Percentile high',             min=0.0, max=100.0, step=0.1),
        prob_thresh     = dict(widget_type='FloatSpinBox', label='Probability/Score Threshold', min=0.0, max=  1.0, step=0.05),
        nms_thresh      = dict(widget_type='FloatSpinBox', label='Overlap Threshold',           min=0.0, max=  1.0, step=0.05),
        output_type     = dict(widget_type='ComboBox', label='Output Type', choices=output_choices),
        label_adv       = dict(widget_type='Label', label='<br>Advanced Options:'),
        n_tiles         = dict(widget_type='LineEdit', label='Number of Tiles'),
        cnn_output      = dict(widget_type='CheckBox', text='Show CNN Output'),
        defaults_button = dict(widget_type='PushButton', text='Restore Defaults'),
        layout          = 'vertical',
        call_button     = True,
    )
    def widget (
        label_head,
        image: 'napari.layers.Image',
        axes,
        label_nn,
        model2d,
        model3d,
        model_folder,
        norm_image,
        perc_low,
        perc_high,
        label_nms,
        prob_thresh,
        nms_thresh,
        output_type,
        label_adv,
        n_tiles,
        cnn_output,
        defaults_button,
        # pbar: ProgressBar,
    ) -> List[napari.types.LayerDataTuple]:

        # for i in range(100):
        #     pbar.increment()

        if _is3D(image):
            if model3d == CUSTOM_MODEL:
                path = Path(model_folder)
                path.exists() or _raise(FileNotFoundError(f"{path} doesn't exist."))
                model = StarDist3D(None, name=path.name, basedir=str(path.parent))
            else:
                model = StarDist3D.from_pretrained(model3d)
        else:
            if model2d == CUSTOM_MODEL:
                path = Path(model_folder)
                path.exists() or _raise(FileNotFoundError(f"{path} doesn't exist."))
                model = StarDist2D(None, name=path.name, basedir=str(path.parent))
            else:
                model = StarDist2D.from_pretrained(model2d)

        x = image.data
        axes = axes_check_and_normalize(axes, length=x.ndim)
        if norm_image:
            # TODO: address joint vs. separate normalization
            if image.rgb == True:
                x = normalize(x, perc_low,perc_high, axis=(0,1,2))
            else:
                x = normalize(x, perc_low,perc_high)

        results = model.predict_instances(x, axes=axes, prob_thresh=prob_thresh, nms_thresh=nms_thresh, return_predict=cnn_output)
        layers = []
        if cnn_output:
            (labels, polys), (prob, dist) = results
            #
            from scipy.ndimage import zoom
            sc = tuple(model.config.grid)
            prob = zoom(prob, sc,      order=0)
            dist = zoom(dist, sc+(1,), order=0)
            dist = np.moveaxis(dist, -1,0)
            layers.append((dist, dict(name='StarDist distances'),   'image'))
            layers.append((prob, dict(name='StarDist probability'), 'image'))
        else:
            labels, polys = results
        n_objects = len(polys['points'])

        if output_type in (Output.Labels.value,Output.Both.value):
            layers.append((labels, dict(name='StarDist labels'), 'labels'))
        if output_type in (Output.Polys.value,Output.Both.value):
            if _is3D(image):
                surface = surface_from_polys(polys)
                layers.append((surface, dict(name='StarDist polyhedra', colormap=label_colormap(n_objects)), 'surface'))
            else:
                # TODO: coordinates correct or need offset (0.5 or so)?
                shapes = np.moveaxis(polys['coord'], 2,1)
                layers.append((shapes, dict(name='StarDist polygons', shape_type='polygon',
                                             edge_width=0.5, edge_color='coral', face_color=[0,0,0,0]), 'shapes'))
        return layers

    # print(widget)

    # restore defaults
    def restore_defaults():
        for k,v in DEFAULTS.items():
            getattr(widget,k).value = v
    restore_defaults()
    widget.defaults_button.changed.connect(lambda e: restore_defaults())

    # ensure that percentile low <= percentile high
    def _perc_low_change(event):
        widget.perc_high.value = max(widget.perc_low.value, widget.perc_high.value)
    def _perc_high_change(event):
        widget.perc_low.value  = min(widget.perc_low.value, widget.perc_high.value)
    widget.perc_low.changed.connect(_perc_low_change)
    widget.perc_high.changed.connect(_perc_high_change)

    # hide percentile selection if normalization turned off
    def _norm_image_change(event):
        widget.perc_low.visible = widget.norm_image.value
        widget.perc_high.visible = widget.norm_image.value
    widget.norm_image.changed.connect(_norm_image_change)

    # show/hide model folder picker
    def _model_change(event):
        if (widget.model2d.visible and widget.model2d.value == CUSTOM_MODEL) or \
           (widget.model3d.visible and widget.model3d.value == CUSTOM_MODEL):
            widget.model_folder.show()
        else:
            widget.model_folder.hide()
    widget.model2d.changed.connect(_model_change)
    widget.model3d.changed.connect(_model_change)

    # show 2d or 3d models (based on guessed image dimensionality)
    def _image_changed(event):
        image = widget.image.get_value()
        if _is3D(image):
            widget.model2d.hide()
            widget.model3d.show()
            widget.axes.value = 'ZYX'
        else:
            widget.model3d.hide()
            widget.model2d.show()
            widget.axes.value = 'YXC' if image.rgb else 'YX'
        _model_change(None)
    widget.image.changed.connect(_image_changed)

    # TODO: check axes and let axes determine dimensionality
    # def _axes_change(event):
    #     print(f'axes = {widget.axes.value}')
    # widget.axes.changed.connect(_axes_change)

    # allow to shrink model selector
    widget.model2d.native.setMinimumWidth(240)
    widget.model3d.native.setMinimumWidth(240)

    # make reset button smaller
    widget.defaults_button.native.setMaximumWidth(150)

    # push 'call_button' to bottom
    layout = widget.native.layout()
    layout.insertStretch(layout.count()-1)

    return widget


@napari_hook_implementation
def napari_experimental_provide_dock_widget():
    return widget_wrapper, {'name': 'StarDist'}