import os
import argparse
from utils_package.quick_start import quick_start
os.environ['NUMEXPR_MAX_THREADS'] = '48'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', '-m', type=str, default='MENTOR', help='name of models')
    parser.add_argument('--dataset', '-d', type=str, default='sports', help='name of datasets')

    config_dict = {
        'gpu_id': 2,
    }

    args, _ = parser.parse_known_args()

    quick_start(model=args.model, dataset=args.dataset, config_dict=config_dict, save_model=True)

