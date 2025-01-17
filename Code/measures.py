import argparse
import copy
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List

import numpy as np
import torch
from bokeh.io import output_notebook, show, output_file, save, reset_output
from bokeh.models import NumeralTickFormatter, Range1d
from bokeh.plotting import figure

import data_loader
import optimizers
from solver import Solver, load_solver


def margins(model: torch.nn.Module, training_loader: data_loader) -> List[float]:
    """
    f_{w}(x)[y_{true}] - max_{y != y_{true}}f_{w}[y]

    :param model:
    :param training_loader:
    :return:
    """
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.eval()
    model.to(device)

    all_margins = []
    wrong_labelled = 0

    with torch.no_grad():
        for data in training_loader:
            inputs, labels = data[0].cuda(device), data[1].numpy()
            outputs = model(inputs).cpu().numpy()

            for i in range(len(labels)):
                predicted = np.argmax(outputs[i])
                correct = labels[i]

                if predicted != correct:
                    wrong_labelled += 1
                correct_labels = outputs[i][labels[i]]
                outputs[i, labels[i]] = -np.inf
                max_other = np.amax(outputs[i])
                margin = correct_labels - max_other
                all_margins.append(margin)

    # print(wrong_labelled, len(all_margins))

    return all_margins


def gamma_margin(model: torch.nn.Module, training_loader: data_loader, eps: float = 0.05) -> float:
    """
    Lowest value of gamma so that ceil(eps * m) data points have a margin lower than gamma.
    m = #datapoints

    :param model:
    :param training_loader:
    :param eps:
    :return:
    """
    all_margins = sorted(margins(model, training_loader))
    eps_m = np.math.ceil(eps * len(all_margins))
    margin = all_margins[eps_m]

    if margin < 0:
        print("Margin is less than 0.")
        # raise ValueError("Margin is less than 0.")

    return margin


def norm_product(layers: OrderedDict, order: int = None, with_hidden: bool = False) -> float:
    """
    Calculates the product of norms over layers.

    :param with_hidden:
    :param layers:
    :param order:
    :return:
    """
    result = 1

    for layer, weights in layers.items():
        if isinstance(weights, torch.Tensor):
            weights = weights.cpu().numpy()

        layer_result = 1

        if layer.endswith('weight'):
            try:
                layer_result = np.linalg.norm(weights, ord=order)
            except ValueError:
                layer_result = 0
                try:
                    for row in weights:
                        for convolution_filter in row:
                            layer_result += np.linalg.norm(convolution_filter, ord=order)
                except ValueError:
                    print("Skipping ", layer)

            if with_hidden:
                layer_result *= weights.shape[0]
        else:
            # print("Skipping %s" % layer)
            pass
        result *= layer_result

    return result


def l2_norm(model: torch.nn.Module, training_loader: data_loader, eps: float = 0.05) -> float:
    """
    Calculates the product of l2 norms over layers scaled by the gamma margin

    :param model:
    :param training_loader:
    :param eps:
    :return:
    """
    margin = gamma_margin(model, training_loader, eps)
    norm = 4 * norm_product(model.state_dict())

    return (margin ** -2) * norm


def spectral_norm(model: torch.nn.Module, training_loader: data_loader, eps: float = 0.05) -> float:
    """
    Calculates the product of spectral norms over layers scaled by the gamma margin

    :param model:
    :param training_loader:
    :param eps:
    :return:
    """
    margin = gamma_margin(model, training_loader, eps)
    norm = norm_product(model.state_dict(), order=2, with_hidden=True)

    return (margin ** -2) * norm


def l1_path_norm(model: torch.nn.Module, training_loader: data_loader, eps: float = 0.05) -> float:
    """
    Calculates the product of l1 path norms over layers scaled by the gamma margin

    :param model:
    :param training_loader:
    :param eps:
    :return:
    """
    margin = gamma_margin(model, training_loader, eps)

    layers = model.state_dict()
    paths, real_model_depth = enumerate_paths(layers, multiply_by_hidden_units=False, power=1, collapse_paths=True)
    paths *= 2 ** real_model_depth
    paths = np.abs(paths)
    norm = paths.sum()
    norm **= 2

    return (margin ** -2) * norm


def l2_path_norm(model: torch.nn.Module, training_loader: data_loader, eps: float = 0.05) -> float:
    """
    Calculates the product of l2 path norms over layers scaled by the gamma margin

    :param model:
    :param training_loader:
    :param eps:
    :return:
    """
    margin = gamma_margin(model, training_loader, eps)

    layers = model.state_dict()

    paths, real_model_depth = enumerate_paths(layers, power=2, multiply_by_hidden_units=True,
                                              collapse_paths=True)
    paths *= 4 ** real_model_depth
    norm = paths.sum()

    return (margin ** -2) * norm


def enumerate_paths(layers: OrderedDict, multiply_by_hidden_units: bool = False, power: int = 1,
                    collapse_paths: bool = True):
    """
    Enumerates all paths in the given layer dict.

    :param collapse_paths:
    :param layers:
    :param multiply_by_hidden_units:
    :param power:
    :return: List of list of weights representing all weights along paths
    """
    real_depth = 0
    paths = np.array([], dtype=np.float64)

    # print("Enumerating paths")

    for layer, weights in reversed(layers.items()):
        if not len(weights.shape) == 2:
            # print("Skipping enumeration of %s layer %s" % (weights.shape, layer))

            continue
        # print("Enumerating %s layer %s" % (weights.shape, layer))
        weights = weights.cpu().numpy()
        rows = weights.shape[0]

        real_depth += 1

        if not paths.any():
            paths = weights.flatten()
            paths **= power

            if multiply_by_hidden_units:
                paths *= rows

            continue

        if not collapse_paths:
            paths = paths.reshape((len(paths) // rows, rows, real_depth - 1))
            paths = paths.transpose((1, 0, 2))
        else:
            paths = paths.reshape((len(paths) // rows, rows)).transpose()

        weights **= power

        if multiply_by_hidden_units:
            weights *= rows

        new_paths = np.empty((0, real_depth + 1), dtype=np.float64)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = []

            for i in range(len(paths)):
                incoming_paths = paths[i]
                outgoing_edges = weights[i % weights.shape[0]]
                futures.append(
                    executor.submit(append_outgoing_edges, incoming_paths, outgoing_edges, real_depth, collapse_paths))

            for future in futures:
                new_paths = np.append(new_paths, future.result())

            if not collapse_paths:
                new_paths = new_paths.reshape((-1, real_depth))

        paths = np.array(new_paths, dtype=np.float64)
        # print("Currently enumerated paths: ", len(paths))

    return paths, real_depth


def append_outgoing_edges(incoming_paths, outgoing_edges, real_depth, collapse_paths):
    """
    Appends the outgoing edges to the incoming paths

    :param incoming_paths:
    :param outgoing_edges:
    :param real_depth:
    :param collapse_paths:
    :return:
    """
    new_paths = np.empty((0, real_depth + 1), dtype=np.float64)

    for element in incoming_paths:
        if not collapse_paths:
            new_column = np.array([element] * len(outgoing_edges), dtype=np.float64)
            new_path = np.column_stack([new_column, outgoing_edges])
            new_paths = np.append(new_paths, new_path).reshape((-1, real_depth))
        else:
            new_path = element * outgoing_edges
            new_paths = np.append(new_paths, new_path)

    return new_paths


def sharpness(model: torch.nn.Module, criterion: Callable, training_loader: data_loader, alpha: float = 5e-4,
              iterations: float = 1e5):
    """
    Calculates the sharpness of the model.
    The sharpness corresponds to robustness to adverserial pertubations on the parameter space.

    :param model:
    :param criterion:
    :param training_loader:
    :param alpha:
    :param iterations:
    :return:
    """
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    real_loss = calculate_loss(model, criterion, training_loader)
    max_loss = -np.inf

    # alpha_interval = [-alpha, -alpha / 2.0, alpha / 2.0, alpha]

    model_parameters = OrderedDict(copy.deepcopy(dict(model.state_dict())))
    scaled_model_parameters = OrderedDict()
    for layer, weights in model_parameters.items():
        p = weights.clone().float()
        p = torch.abs(p)
        p += 1.0
        p *= alpha
        scaled_model_parameters[layer] = p

    pertubated_parameters = OrderedDict()
    pertubation = OrderedDict()

    for iteration in range(int(iterations)):
        if not iteration % 1:  # 50:
            print(iteration)

        for layer, weights in model_parameters.items():
            r = torch.rand(weights.shape).to(device)
            r *= 2.0
            r -= 1.0
            pertubation[layer] = scaled_model_parameters[layer] * r

        for layer, weights in model_parameters.items():
            pertubated_parameters[layer] = model_parameters[layer] + pertubation[layer]

        model.load_state_dict(pertubated_parameters)
        loss = calculate_loss(model, criterion, training_loader) - real_loss

        if loss > max_loss:
            max_loss = loss
            print_norm("Sharpness", max_loss)

    return max_loss

    # for layer in model.state_dict().keys():
    #     for row in range(len(model.state_dict()[layer])):
    #         if not len(model.state_dict()[layer][row].shape):
    #             pertubate_bias(model, criterion, training_loader, alpha_interval, real_loss, layer, row)
    #         else:
    #             for column in range(len(model.state_dict()[layer][row])):
    #                 print()


# def pertubate_bias(model: torch.nn.Module, criterion: Callable, training_loader: data_loader, alpha_interval: list,
#                    real_loss: float, layer: str, row: int) -> float:
#     """
#     Pertubates a bias layer and returns the max sharpness loss
#
#     :param layer:
#     :param real_loss:
#     :param row:
#     :param model:
#     :param criterion:
#     :param training_loader:
#     :param alpha_interval:
#     :return:
#     """
#     max_loss = -np.inf
#     original_value = model.state_dict()[layer][row].item()
#     for v in alpha_interval:
#         model.state_dict()[layer][row] = original_value + v
#         print(model.state_dict()[layer][row].item())
#         loss = calculate_loss(model, criterion, training_loader)
#
#         if loss > max_loss:
#             max_loss
#
#     model.state_dict()[layer][row] = original_value


def calculate_loss(model: torch.nn.Module, criterion: Callable, training_loader: data_loader):
    """
    Calculates the loss of the model.

    :param model:
    :param criterion:
    :param training_loader:
    :return:
    """
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()

    with torch.no_grad():
        total_loss = 0.0

        for i, data in enumerate(training_loader, 0):
            inputs, labels = data[0].to(device), data[1].to(device)
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            total_loss += loss.item()

        total_loss /= len(training_loader)

    return total_loss


def print_norm(norm, value):
    print("{:15}: {:150.30f}".format(norm, value))


def calculate_all_measures(_solver: Solver):
    """

    :param _solver:
    :return:
    """
    model = _solver.model
    model.load_state_dict(_solver.model_state)
    data = dict(_solver.data)
    training_loader = getattr(data_loader, data['dataset'])
    del data['dataset']
    training_loader = training_loader(True, **data)

    eps = 0.05

    l2 = 0  # l2_norm(model, training_loader, eps)
    print_norm("L2", l2)
    spectral = 0  # spectral_norm(model, training_loader, eps)
    print_norm("Spectral", spectral)
    try:
        l2_path = 0  # l2_path_norm(model, training_loader, eps)
        print_norm("L2-path", l2_path)
    except ValueError:
        print("l2-path norm does not exist")
    try:
        l1_path = 0  # l1_path_norm(model, training_loader, eps)
        print_norm("L1-path", l1_path)
    except ValueError:
        print("l1-path norm does not exist")

    criterion = getattr(optimizers, _solver.strategy['criterion'])()
    s = sharpness(model, criterion, training_loader, alpha=5e-4, iterations=1e100)
    print_norm("Sharpness", s)

    return l2, spectral, l2_path, l1_path, s


def plot(title, x_labels,
         y_range,
         lines=None,
         x_axis_label='Size of training set',
         legend_location='top_left',
         y_axis_type="log",
         x_axis_type="linear",
         output_name=None,
         show_plot=False):
    """

    :param title:
    :param x_labels:
    :param y_range:
    :param lines:
    :param x_axis_label:
    :param legend_location:
    :param y_axis_type:
    :param x_axis_type:
    :param output_name:
    :param show_plot:
    :return:
    """
    reset_output()
    output_file('/mnt/nextcloud/tum/Master/3.Semester/Seminar/Presentation/html/' + output_name.replace(' ', ''))

    if lines is None:
        lines = []
    if not output_name:
        output_name = title
    p = figure(title=title, x_axis_label=x_axis_label, y_range=Range1d(*y_range), y_axis_type=y_axis_type,
               x_axis_type=x_axis_type,
               width=int(800 * .7), height=int(600 * .7))
    for line in lines:
        y = line['y']
        legend_label = line['legend_label']
        t = line['type']
        line_color = line['line_color']
        fill_color = line['fill_color']
        p.line(x_labels, y, legend_label=legend_label, line_color=line_color, line_width=2, line_alpha=1)
        getattr(p, t)(x_labels, y, legend_label=legend_label, line_color=line_color, fill_color=fill_color,
                      line_width=4, size=6, line_alpha=1)
    p.xaxis.formatter = NumeralTickFormatter(format="0a")
    p.title.text_font_size = '16pt'
    p.xaxis.axis_label_text_font_size = '16pt'
    p.xaxis.major_label_text_font_size = '16pt'
    p.yaxis.axis_label_text_font_size = '16pt'
    p.yaxis.major_label_text_font_size = '16pt'
    p.legend.location = legend_location
    p.xaxis.ticker = x_labels
    p.legend.click_policy = "hide"
    if show_plot:
        output_notebook()
        show(p)

    save(p)
    return p


def parse_measures(folder, filename):
    """

    :param folder:
    :param filename:
    :return:
    """
    if not folder.endswith('/'):
        folder += '/'
    with open(folder + filename, 'r') as file:
        data = file.read()

        name = (data[:data.find('\n')]).split('/')[-2]

        l2, spectral, l2_path, l1_path, sharpness = 0, 0, 0, 0, 0

        if not data.find('L2') < 0:
            data = data[data.find('L2') + len('L2'):].strip()
            data = data[data.find(':') + len(':'):].strip()
            l2 = float(data[:data.find('\n')])

        if not data.find('Spectral') < 0:
            data = data[data.find('Spectral') + len('Spectral'):].strip()
            data = data[data.find(':') + len(':'):].strip()
            spectral = float(data[:data.find('\n')])

        if not data.find('L2-path') < 0:
            data = data[data.find('L2-path') + len('L2-path'):].strip()
            data = data[data.find(':') + len(':'):].strip()
            l2_path = float(data[:data.find('\n')])

        if not data.find('L1-path') < 0:
            data = data[data.find('L1-path') + len('L1-path'):].strip()
            data = data[data.find(':') + len(':'):].strip()
            l1_path = float(data[:data.find('\n')])

        if not data.rfind('Sharpness') < 0:
            data = data[data.rfind('Sharpness') + len('Sharpness'):].strip()
            data = data[data.find(':') + len(':'):].strip()
            sharpness = float(data[:data.find('\n')])

    return {'name': name,
            'l2': l2,
            'spectral': spectral,
            'l2_path': l2_path,
            'l1_path': l1_path,
            'sharpness': sharpness}


if __name__ == '__main__':
    # initiate the parser
    PARSER = argparse.ArgumentParser()

    FOLDER = '/storage/slurm/brucknem/'
    PARSER.add_argument("--dir",
                        "-d",
                        help="specify the directory (default: %s)" % FOLDER)

    FILENAME = 'solver_e_reached_100.pth'
    PARSER.add_argument("--file",
                        "-f",
                        help="specify the filename (default: %s)" % FILENAME)

    # read arguments from the command line
    ARGS = PARSER.parse_args()

    FOLDER = ARGS.dir if ARGS.dir is not None else FOLDER
    FILENAME = ARGS.file if ARGS.file is not None else FILENAME

    if not FOLDER.endswith('/'):
        FOLDER += '/'

    solver = load_solver(folder=FOLDER, filename=FILENAME)
    calculate_all_measures(solver)
