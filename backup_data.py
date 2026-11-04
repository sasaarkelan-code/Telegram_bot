"""Run before upgrading; copies stay on the server, outside the application data."""
import argparse
import os
from pathlib import Path
from smm.backup import backup_data

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Локальная резервная копия данных SMM')
    parser.add_argument('--data',default=os.environ.get('SMM_DATA_DIR','/app/data/smm'))
    parser.add_argument('--output',default=None)
    args=parser.parse_args()
    print('Резервная копия:',backup_data(Path(args.data),args.output))
    print('Архив содержит входы и сессии. Храни копию приватно; не загружай в GitHub.')
