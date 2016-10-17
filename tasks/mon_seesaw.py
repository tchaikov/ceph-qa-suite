import contextlib
import logging
import random

import ceph_manager
from teuthology import misc as teuthology
from teuthology.orchestra import run


log = logging.getLogger(__name__)


def _get_mons(ctx):
    return [name[len('mon.'):] for name in teuthology.get_mon_names(ctx)]


@contextlib.contextmanager
def _prepare_mon(ctx, manager, mon, remote):
    cluster = manager.cluster
    data_path = '/var/lib/ceph/mon/{cluster}-{id}'.format(
        cluster=cluster, id=mon)
    remote.run(args=['sudo', 'mkdir', '-p', data_path])
    keyring_path = '/etc/ceph/{cluster}.keyring'.format(
        cluster=manager.cluster)
    testdir = teuthology.get_testdir(ctx)
    monmap_path = '{tdir}/{cluster}.monmap'.format(tdir=testdir,
                                                   cluster=cluster)
    manager.raw_cluster_cmd('mon', 'getmap', '-o', monmap_path)
    remote.run(
        args=[
            'sudo',
            'ceph-mon',
            '--cluster', cluster,
            '--mkfs',
            '-i', mon,
            '--monmap', monmap_path,
            '--keyring', keyring_path])
    ctx.ceph[cluster].conf['mon.{0}'.format{mon}] = {
        'mon addr': 
    }
    remote.run(args=['sudo', 'rm', '--', monmap_path])
    yield
    remote.run(args=['sudo', 'rm', '-rf', data_path])


# run_daemon() starts a herd of daemons of the same type, but _run_daemon()
# starts only one instance.
@contextlib.contextmanager
def _run_daemon(ctx, config, remote, type_, id_):
    cluster_name = config['cluster']
    testdir = teuthology.get_testdir(ctx)
    coverage_dir = '{tdir}/archive/coverage'.format(tdir=testdir)
    daemon_signal = 'kill'
    run_cmd = [
        'sudo',
        'adjust-ulimits',
        'ceph-coverage',
        coverage_dir,
        'daemon-helper',
        daemon_signal,
    ]
    run_cmd_tail = [
        'ceph-%s' % (type_),
        '-f',
        '--cluster', cluster_name,
        '-i', id_]
    run_cmd.extend(run_cmd_tail)
    ctx.daemons.add_daemon(remote, type_, id_,
                           cluster=cluster_name,
                           args=run_cmd,
                           logger=log.getChild(type_),
                           stdin=run.PIPE,
                           wait=False)
    daemon = ctx.daemons.get_daemon(type_, id_, cluster_name)
    yield daemon
    daemon.stop()


@contextlib.contextmanager
def task(ctx, config):
    """
    replace a monitor with a newly added one, and then revert this change

    How it works::
    1. add a mon with specified id (mon.victim_prime)
    2. wait for quorum
    3. remove a monitor with specified id (mon.victim), mon.victim will commit
       suicide
    4. wait for quorum
    5. <yield>
    5. add mon.a back, and start it
    6. wait for quorum
    7. remove mon.a_prime

    Options::
    victim       the id of the mon to be removed (pick a random mon by default)
    replacer     the id of the new mon (use "${victim}_prime" if not specified)
    """
    first_mon = teuthology.get_first_mon(ctx, config)
    (mon,) = ctx.cluster.only(first_mon).remotes.iterkeys()
    manager = ceph_manager.CephManager(
        mon,
        ctx=ctx,
        logger=log.getChild('ceph_manager'))

    if config is None:
        config = {}
    assert isinstance(config, dict), \
        "task ceph only supports a dictionary for configuration"
    overrides = ctx.config.get('overrides', {})
    teuthology.deep_merge(config, overrides.get('mon_seesaw', {}))
    victim = config.get('victim', random.choice(_get_mons(ctx)))
    replacer = config.get('replacer', '{0}_prime'.format(victim))
    remote = manager.find_remote('mon', victim)
    log.info('replacing {victim} with {replacer}'.format(victim=victim,
                                                         replacer=replacer))
    with _prepare_mon(ctx, manager, replacer, remote):
        quorum = manager.get_mon_quorum()
        with _run_daemon(ctx, config, remote, 'mon', replacer):
            # replacer will join the quorum automatically
            manager.wait_for_mon_quorum_size(len(quorum) + 1)
            log.info('removing {mon}'.format(mon=victim))
            manager.raw_cluster_cmd("mon", "remove", victim)
            manager.wait_for_mon_quorum_size(len(quorum))
            cluster = manager.cluster
            ctx.daemons.get_daemon('mon', mon, cluster).wait(10)
            try:
                yield
            finally:
                addr = ctx.ceph[cluster].conf['mon.%s' % victim]['mon addr']
                manager.raw_cluster_cmd("mon", "add", victim, addr)
                log.info('reviving {mon}'.format(mon=victim))
                manager.revive_mon(victim)
                manager.wait_for_mon_quorum_size(len(quorum) + 1)

