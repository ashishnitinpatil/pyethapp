import monkeypatches
import json
import sys
import os
import signal
import click
from click import BadParameter
import gevent
from gevent.event import Event
from devp2p.service import BaseService
from devp2p.peermanager import PeerManager
from devp2p.discovery import NodeDiscovery
from devp2p.app import BaseApp
from eth_service import ChainService
from console_service import Console
from ethereum.blocks import Block
import ethereum.slogging as slogging
import config as konfig
from db_service import DBService
from jsonrpc import JSONRPCServer
from pow_service import PoWService
from accounts import AccountsService
from pyethapp import __version__
import utils

slogging.configure(config_string=':debug')
log = slogging.get_logger('app')


services = [DBService, AccountsService, NodeDiscovery, PeerManager, ChainService, PoWService,
            JSONRPCServer, Console]


class EthApp(BaseApp):
    client_version = 'pyethapp/v%s/%s/%s' % (__version__, sys.platform,
                                             'py%d.%d.%d' % sys.version_info[:3])
    default_config = dict(BaseApp.default_config)
    default_config['client_version'] = client_version
    default_config['post_app_start_callback'] = None


@click.group(help='Welcome to ethapp version:{}'.format(EthApp.client_version))
@click.option('alt_config', '--Config', '-C', type=click.File(), help='Alternative config file')
@click.option('config_values', '-c', multiple=True, type=str,
              help='Single configuration parameters (<param>=<value>)')
@click.option('data_dir', '--data-dir', '-d', multiple=False, type=str,
              help='data directory')
@click.option('log_config', '--log_config', '-l', multiple=False, type=str,
              help='log_config string: e.g. ":info,eth:debug')
@click.option('--log-json/--log-no-json', default=False,
              help='log as structured json output')
@click.option('bootstrap_node', '--bootstrap_node', '-b', multiple=False, type=str,
              help='single bootstrap_node as enode://pubkey@host:port')
@click.option('mining_pct', '--mining_pct', '-m', multiple=False, type=int, default=0,
              help='pct cpu used for mining')
@click.pass_context
def app(ctx, alt_config, config_values, data_dir, log_config, bootstrap_node, log_json,
        mining_pct):

    # configure logging
    log_config = log_config or ':info'
    slogging.configure(log_config, log_json=log_json)

    # data dir default or from cli option
    data_dir = data_dir or konfig.default_data_dir
    konfig.setup_data_dir(data_dir)  # if not available, sets up data_dir and required config
    log.info('using data in', path=data_dir)

    # prepare configuration
    # config files only contain required config (privkeys) and config different from the default
    if alt_config:  # specified config file
        config = konfig.load_config(alt_config)
    else:  # load config from default or set data_dir
        config = konfig.load_config(data_dir)

    config['data_dir'] = data_dir

    # add default config
    konfig.update_config_with_defaults(config, konfig.get_default_config([EthApp] + services))

    # override values with values from cmd line
    for config_value in config_values:
        try:
            konfig.set_config_param(config, config_value)
            # check if this is part of the default config
        except ValueError:
            raise BadParameter('Config parameter must be of the form "a.b.c=d" where "a.b.c" '
                               'specifies the parameter to set and d is a valid yaml value '
                               '(example: "-c jsonrpc.port=5000")')
    if bootstrap_node:
        config['discovery']['bootstrap_nodes'] = [bytes(bootstrap_node)]
    if mining_pct > 0:
        config['pow']['activated'] = True
        config['pow']['cpu_pct'] = int(min(100, mining_pct))
    if not config['pow']['activated']:
        config['deactivated_services'].append(PoWService.name)

    ctx.obj = {'config': config}


@app.command()
@click.option('--dev/--nodev', default=False, help='Exit at unhandled exceptions')
@click.option('--nodial/--dial',  default=False, help='do not dial nodes')
@click.option('--fake/--nofake',  default=False, help='fake genesis difficulty')
@click.pass_context
def run(ctx, dev, nodial, fake):
    """Start the client ( --dev to stop on error)"""
    config = ctx.obj['config']
    if nodial:
        # config['deactivated_services'].append(PeerManager.name)
        # config['deactivated_services'].append(NodeDiscovery.name)
        config['discovery']['bootstrap_nodes'] = []
        config['discovery']['listen_port'] = 29873
        config['p2p']['listen_port'] = 29873
        config['p2p']['min_peers'] = 0

    if fake:
        from ethereum import blocks
        blocks.GENESIS_DIFFICULTY = 1024
        blocks.BLOCK_DIFF_FACTOR = 16
    # create app
    app = EthApp(config)

    # development mode
    if dev:
        gevent.get_hub().SYSTEM_ERROR = BaseException
        try:
            config['client_version'] += '/' + os.getlogin()
        except:
            log.warn("can't get and add login name to client_version")
            pass

    # dump config
    konfig.dump_config(config)

    # register services
    for service in services:
        assert issubclass(service, BaseService)
        if service.name not in app.config['deactivated_services']:
            assert service.name not in app.services
            service.register_with_app(app)
            assert hasattr(app.services, service.name)

    # start app
    log.info('starting')
    app.start()

    if config['post_app_start_callback'] is not None:
        config['post_app_start_callback'](app)

    # wait for interrupt
    evt = Event()
    gevent.signal(signal.SIGQUIT, evt.set)
    gevent.signal(signal.SIGTERM, evt.set)
    gevent.signal(signal.SIGINT, evt.set)
    evt.wait()

    # finally stop
    app.stop()


@app.command()
@click.pass_context
def config(ctx):
    """Show the config"""
    konfig.dump_config(ctx.obj['config'])


@app.command()
@click.argument('file', type=click.File(), required=True)
@click.argument('name', type=str, required=True)
@click.pass_context
def blocktest(ctx, file, name):
    """Start after importing blocks from a file.

    In order to prevent replacement of the local test chain by the main chain from the network, the
    peermanager, if registered, is stopped before importing any blocks.

    Also, for block tests an in memory database is used. Thus, a already persisting chain stays in
    place.
    """
    app = EthApp(ctx.obj['config'])
    app.config['db']['implementation'] = 'EphemDB'

    # register services
    for service in services:
        assert issubclass(service, BaseService)
        if service.name not in app.config['deactivated_services']:
            assert service.name not in app.services
            service.register_with_app(app)
            assert hasattr(app.services, service.name)

    if ChainService.name not in app.services:
        log.fatal('No chainmanager registered')
        ctx.abort()
    if DBService.name not in app.services:
        log.fatal('No db registered')
        ctx.abort()

    log.info('loading block file', path=file.name)
    try:
        data = json.load(file)
    except ValueError:
        log.fatal('Invalid JSON file')
    if name not in data:
        log.fatal('Name not found in file')
        ctx.abort()
    try:
        blocks = utils.load_block_tests(data.values()[0], app.services.chain.chain.db)
    except ValueError:
        log.fatal('Invalid blocks encountered')
        ctx.abort()

    # start app
    app.start()
    if 'peermanager' in app.services:
        app.services.peermanager.stop()

    log.info('building blockchain')
    Block.is_genesis = lambda self: self.number == 0
    app.services.chain.chain._initialize_blockchain(genesis=blocks[0])
    for block in blocks[1:]:
        app.services.chain.chain.add_block(block)

    # wait for interrupt
    evt = Event()
    gevent.signal(signal.SIGQUIT, evt.set)
    gevent.signal(signal.SIGTERM, evt.set)
    gevent.signal(signal.SIGINT, evt.set)
    evt.wait()

    # finally stop
    app.stop()


@app.command()
@click.option('--from', 'from_', type=int, help='The first block to export (default: genesis)')
@click.option('--to', type=int, help='The last block to export (default: latest)')
@click.argument('file', type=click.File('wb'))
@click.pass_context
def export(ctx, from_, to, file):
    """Export the blockchain to <FILE>.

    The chain will be stored in binary format, i.e. as a concatenated list of RLP encoded blocks,
    starting with the earliest block.
    """
    app = EthApp(ctx.obj['config'])
    DBService.register_with_app(app)
    AccountsService.register_with_app(app)
    ChainService.register_with_app(app)

    if from_ is None:
        from_ = 0
    head_number = app.services.chain.chain.head.number
    if to is None:
        to = head_number
    if from_ < 0:
        log.fatal('block numbers must not be negative')
        sys.exit(1)
    if to < from_:
        log.fatal('"to" block must be newer than "from" block')
        sys.exit(1)
    if to > head_number:
        log.fatal('"to" block not known (current head: {})'.format(head_number))
        sys.exit(1)

    log.info('Starting export')
    for n in xrange(from_, to + 1):
        log.debug('Exporting block {}'.format(n))
        if (n - from_) % 50000 == 0:
            log.info('Exporting block {} to {}'.format(n, min(n + 50000, to)))
        block_hash = app.services.chain.chain.index.get_block_by_number(n)
        # bypass slow block decoding by directly accessing db
        block_rlp = app.services.db.get(block_hash)
        file.write(block_rlp)
    log.info('Export complete')


if __name__ == '__main__':
    #  python app.py 2>&1 | less +F
    app()
