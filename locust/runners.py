# -*- coding: utf-8 -*-
import logging
import random
import socket
import traceback
import warnings
from hashlib import md5
from time import time

import gevent
import six
from gevent import GreenletExit
from gevent.pool import Group

from six.moves import xrange

from . import events
from .rpc import Message, rpc
from .stats import global_stats

import yaml


logger = logging.getLogger(__name__)

# global locust runner singleton
locust_runner = None

STATE_INIT, STATE_HATCHING, STATE_RUNNING, STATE_CLEANUP, STATE_STOPPED = ["ready", "hatching", "running", "cleanup", "stopped"]
SLAVE_REPORT_INTERVAL = 3.0


class LocustRunner(object):
    def __init__(self, locust_classes, options):
        self.options = options
        self.locust_classes = locust_classes
        self.hatch_rate = options.hatch_rate
        self.num_clients = options.num_clients
        self.host = options.host
        self.locusts = Group()
        self.greenlet = self.locusts
        self.state = STATE_INIT
        self.hatching_greenlet = None
        self.exceptions = {}
        self.stats = global_stats

        # register listener that resets stats when hatching is complete
        def on_hatch_complete(user_count):
            self.state = STATE_RUNNING
            if self.options.reset_stats:
                logger.info("Resetting stats\n")
                self.stats.reset_all()
        events.hatch_complete += on_hatch_complete

    @property
    def request_stats(self):
        return self.stats.entries

    @property
    def errors(self):
        return self.stats.errors

    @property
    def user_count(self):
        return len(self.locusts)

    def weight_locusts(self, amount, stop_timeout = None):
        """
        Distributes the amount of locusts for each WebLocust-class according to it's weight
        returns a list "bucket" with the weighted locusts
        """
        bucket = []
        weight_sum = sum((locust.weight for locust in self.locust_classes if locust.task_set))
        for locust in self.locust_classes:
            if not locust.task_set:
                warnings.warn("Notice: Found Locust class (%s) got no task_set. Skipping..." % locust.__name__)
                continue

            if self.host is not None:
                locust.host = self.host
            if stop_timeout is not None:
                locust.stop_timeout = stop_timeout

            # create locusts depending on weight
            percent = locust.weight / float(weight_sum)
            num_locusts = int(round(amount * percent))
            bucket.extend([locust for x in xrange(0, num_locusts)])
        return bucket

    def spawn_locusts(self, spawn_count=None, stop_timeout=None, wait=False):
        if spawn_count is None:
            spawn_count = self.num_clients

        bucket = self.weight_locusts(spawn_count, stop_timeout)
        spawn_count = len(bucket)
        if self.state == STATE_INIT or self.state == STATE_STOPPED:
            self.state = STATE_HATCHING
            self.num_clients = spawn_count
        else:
            self.num_clients += spawn_count

        logger.info("Hatching and swarming %i clients at the rate %g clients/s..." % (spawn_count, self.hatch_rate))
        occurence_count = dict([(l.__name__, 0) for l in self.locust_classes])

        def hatch():
            sleep_time = 1.0 / self.hatch_rate
            while True:
                if not bucket:
                    logger.info("All locusts hatched: %s" % ", ".join(["%s: %d" % (name, count) for name, count in six.iteritems(occurence_count)]))
                    events.hatch_complete.fire(user_count=self.num_clients)
                    return

                locust = bucket.pop(random.randint(0, len(bucket)-1))
                occurence_count[locust.__name__] += 1
                def start_locust(_):
                    try:
                        locust().run(runner=self)
                    except GreenletExit:
                        pass
                new_locust = self.locusts.spawn(start_locust, locust)
                if len(self.locusts) % 10 == 0:
                    logger.debug("%i locusts hatched" % len(self.locusts))
                gevent.sleep(sleep_time)

        hatch()
        if wait:
            self.locusts.join()
            logger.info("All locusts dead\n")

    def kill_locusts(self, kill_count):
        """
        Kill a kill_count of weighted locusts from the Group() object in self.locusts
        """
        bucket = self.weight_locusts(kill_count)
        kill_count = len(bucket)
        self.num_clients -= kill_count
        logger.info("Killing %i locusts" % kill_count)
        dying = []
        for g in self.locusts:
            for l in bucket:
                if l == g.args[0]:
                    dying.append(g)
                    bucket.remove(l)
                    break
        for g in dying:
            self.locusts.killone(g)
        events.hatch_complete.fire(user_count=self.num_clients)

    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False):
        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            self.stats.clear_all()
            self.stats.start_time = time()
            self.exceptions = {}
            events.locust_start_hatching.fire()

        # Dynamically changing the locust count
        if self.state != STATE_INIT and self.state != STATE_STOPPED:
            self.state = STATE_HATCHING
            if self.num_clients > locust_count:
                # Kill some locusts
                kill_count = self.num_clients - locust_count
                self.kill_locusts(kill_count)
            elif self.num_clients < locust_count:
                # Spawn some locusts
                if hatch_rate:
                    self.hatch_rate = hatch_rate
                spawn_count = locust_count - self.num_clients
                self.spawn_locusts(spawn_count=spawn_count)
            else:
                events.hatch_complete.fire(user_count=self.num_clients)
        else:
            if hatch_rate:
                self.hatch_rate = hatch_rate
            if locust_count is not None:
                self.spawn_locusts(locust_count, wait=wait)
            else:
                self.spawn_locusts(wait=wait)

    def stop(self):
        # if we are currently hatching locusts we need to kill the hatching greenlet first
        if self.hatching_greenlet and not self.hatching_greenlet.ready():
            self.hatching_greenlet.kill(block=True)
        self.locusts.kill(block=True)
        self.state = STATE_STOPPED
        events.locust_stop_hatching.fire()

    def quit(self):
        self.stop()
        self.greenlet.kill(block=True)

    def log_exception(self, node_id, msg, formatted_tb):
        key = hash(formatted_tb)
        row = self.exceptions.setdefault(key, {"count": 0, "msg": msg, "traceback": formatted_tb, "nodes": set()})
        row["count"] += 1
        row["nodes"].add(node_id)
        self.exceptions[key] = row

class LocalLocustRunner(LocustRunner):
    def __init__(self, locust_classes, options):
        super(LocalLocustRunner, self).__init__(locust_classes, options)

        # register listener thats logs the exception for the local runner
        def on_locust_error(locust_instance, exception, tb):
            formatted_tb = "".join(traceback.format_tb(tb))
            self.log_exception("local", str(exception), formatted_tb)
        events.locust_error += on_locust_error

    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False):
        self.hatching_greenlet = gevent.spawn(lambda: super(LocalLocustRunner, self).start_hatching(locust_count, hatch_rate, wait=wait))
        self.greenlet = self.hatching_greenlet

class DistributedLocustRunner(LocustRunner):
    def __init__(self, locust_classes, options):
        super(DistributedLocustRunner, self).__init__(locust_classes, options)
        self.master_host = options.master_host
        self.master_port = options.master_port
        self.master_bind_host = options.master_bind_host
        self.master_bind_port = options.master_bind_port

    def noop(self, *args, **kwargs):
        """ Used to link() greenlets to in order to be compatible with gevent 1.0 """
        pass

class SlaveNode(object):
    def __init__(self, id, state=STATE_INIT):
        self.id = id
        self.state = state
        self.user_count = 0

class MasterLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(MasterLocustRunner, self).__init__(*args, **kwargs)

        class SlaveNodesDict(dict):
            def get_by_state(self, state):
                return [c for c in six.itervalues(self) if c.state == state]

            @property
            def ready(self):
                return self.get_by_state(STATE_INIT)

            @property
            def hatching(self):
                return self.get_by_state(STATE_HATCHING)

            @property
            def running(self):
                return self.get_by_state(STATE_RUNNING)

        self.clients = SlaveNodesDict()
        self.server = rpc.Server(self.master_bind_host, self.master_bind_port)
        self.greenlet = Group()
        self.greenlet.spawn(self.client_listener).link_exception(callback=self.noop)

        # listener that gathers info on how many locust users the slaves has spawned
        def on_slave_report(client_id, data):
            if client_id not in self.clients:
                logger.info("Discarded report from unrecognized slave %s", client_id)
                return

            self.clients[client_id].user_count = data["user_count"]
        events.slave_report += on_slave_report

        # register listener that sends quit message to slave nodes
        def on_quitting():
            self.quit()
        events.quitting += on_quitting

    @property
    def user_count(self):
        return sum([c.user_count for c in six.itervalues(self.clients)])

    def start_hatching(self, locust_count, hatch_rate):
        num_slaves = len(self.clients.ready) + len(self.clients.running)
        if not num_slaves:
            logger.warning("You are running in distributed mode but have no slave servers connected. "
                           "Please connect slaves prior to swarming.")
            return

        self.num_clients = locust_count
        slave_num_clients = locust_count // (num_slaves or 1)
        slave_hatch_rate = float(hatch_rate) / (num_slaves or 1)
        remaining = locust_count % num_slaves

        logger.info("Sending hatch jobs to %d ready clients", num_slaves)

        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            self.stats.clear_all()
            self.exceptions = {}
            events.master_start_hatching.fire()

        for client in six.itervalues(self.clients):
            data = {
                "hatch_rate":slave_hatch_rate,
                "num_clients":slave_num_clients,
                "host":self.host,
                "stop_timeout":None
            }

            if remaining > 0:
                data["num_clients"] += 1
                remaining -= 1

            self.server.send(Message("hatch", data, None))

        self.stats.start_time = time()
        self.state = STATE_HATCHING

    def stop(self):
        for client in self.clients.hatching + self.clients.running:
            self.server.send(Message("stop", None, None))
        events.master_stop_hatching.fire()

    def quit(self):
        for client in six.itervalues(self.clients):
            self.server.send(Message("quit", None, None))
        self.greenlet.kill(block=True)

    def client_listener(self):
        while True:
            msg = self.server.recv()
            if msg.type == "client_ready":
                id = msg.node_id
                self.clients[id] = SlaveNode(id)
                logger.info("Client %r reported as ready. Currently %i clients ready to swarm." % (id, len(self.clients.ready)))
                ## emit a warning if the slave's clock seem to be out of sync with our clock
                #if abs(time() - msg.data["time"]) > 5.0:
                #    warnings.warn("The slave node's clock seem to be out of sync. For the statistics to be correct the different locust servers need to have synchronized clocks.")
            elif msg.type == "client_stopped":
                del self.clients[msg.node_id]
                if len(self.clients.hatching + self.clients.running) == 0:
                    self.state = STATE_STOPPED
                logger.info("Removing %s client from running clients" % (msg.node_id))
            elif msg.type == "stats":
                events.slave_report.fire(client_id=msg.node_id, data=msg.data)
            elif msg.type == "hatching":
                self.clients[msg.node_id].state = STATE_HATCHING
            elif msg.type == "hatch_complete":
                self.clients[msg.node_id].state = STATE_RUNNING
                self.clients[msg.node_id].user_count = msg.data["count"]
                if len(self.clients.hatching) == 0:
                    count = sum(c.user_count for c in six.itervalues(self.clients))
                    events.hatch_complete.fire(user_count=count)
            elif msg.type == "quit":
                if msg.node_id in self.clients:
                    del self.clients[msg.node_id]
                    logger.info("Client %r quit. Currently %i clients connected." % (msg.node_id, len(self.clients.ready)))
            elif msg.type == "exception":
                self.log_exception(msg.node_id, msg.data["msg"], msg.data["traceback"])

    @property
    def slave_count(self):
        return len(self.clients.ready) + len(self.clients.hatching) + len(self.clients.running)

class SlaveLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(SlaveLocustRunner, self).__init__(*args, **kwargs)
        self.client_id = socket.gethostname() + "_" + md5(str(time() + random.randint(0,10000)).encode('utf-8')).hexdigest()

        self.client = rpc.Client(self.master_host, self.master_port)
        self.greenlet = Group()

        self.greenlet.spawn(self.worker).link_exception(callback=self.noop)
        self.client.send(Message("client_ready", None, self.client_id))
        self.greenlet.spawn(self.stats_reporter).link_exception(callback=self.noop)

        # register listener for when all locust users have hatched, and report it to the master node
        def on_hatch_complete(user_count):
            self.client.send(Message("hatch_complete", {"count":user_count}, self.client_id))
        events.hatch_complete += on_hatch_complete

        # register listener that adds the current number of spawned locusts to the report that is sent to the master node
        def on_report_to_master(client_id, data):
            data["user_count"] = self.user_count
        events.report_to_master += on_report_to_master

        # register listener that sends quit message to master
        def on_quitting():
            self.client.send(Message("quit", None, self.client_id))
        events.quitting += on_quitting

        # register listener thats sends locust exceptions to master
        def on_locust_error(locust_instance, exception, tb):
            formatted_tb = "".join(traceback.format_tb(tb))
            self.client.send(Message("exception", {"msg" : str(exception), "traceback" : formatted_tb}, self.client_id))
        events.locust_error += on_locust_error

    def worker(self):
        while True:
            msg = self.client.recv()
            if msg.type == "hatch":
                self.client.send(Message("hatching", None, self.client_id))
                job = msg.data
                self.hatch_rate = job["hatch_rate"]
                #self.num_clients = job["num_clients"]
                self.host = job["host"]
                self.hatching_greenlet = gevent.spawn(lambda: self.start_hatching(locust_count=job["num_clients"], hatch_rate=job["hatch_rate"]))
            elif msg.type == "stop":
                self.stop()
                self.client.send(Message("client_stopped", None, self.client_id))
                self.client.send(Message("client_ready", None, self.client_id))
            elif msg.type == "quit":
                logger.info("Got quit message from master, shutting down...")
                self.stop()
                self.greenlet.kill(block=True)

    def stats_reporter(self):
        while True:
            data = {}
            events.report_to_master.fire(client_id=self.client_id, data=data)
            try:
                self.client.send(Message("stats", data, self.client_id))
            except:
                logger.error("Connection lost to master server. Aborting...")
                break

            gevent.sleep(SLAVE_REPORT_INTERVAL)


class K8sLocustRunner(LocustRunner):

    def __init__(self, options):
        from kubernetes import client as k8s_client, config
        self.k8s_client = k8s_client

        # we need to differenciate how the master uses master_host to
        # bind to an IP and how we configure the slaves to connect to the master

        # this property has the real master ip to be set to the slaves
        self.master_host_k8s = options.master_host

        # for the master process itself, let's just bind to all
        # available IPs. we must keep this with a value bindable ip for
        # compatibility reasons. See: SlaveLocustRunner.__init__
        options.master_host = '0.0.0.0'

        super(K8sLocustRunner, self).__init__([], options)

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

        raw_deployment_definition = yaml.load(
            open(options.deployment_filename).read()
        )

        self.raw_deployment_definition = raw_deployment_definition
        self.namespace = None
        self.slave_deployment = None

    def setup_namespace(self):
        """
        Creates an namespace with an autogenerated name inside k8s
        """
        namespace = self.k8s_client.CoreV1Api().create_namespace(
            body=self.k8s_client.V1Namespace(
                metadata=self.k8s_client.V1ObjectMeta(
                    generate_name="locust-loadtest-"
                )
            )
        )

        # give some time for the api to process the namespace
        gevent.sleep(5)

        return namespace.metadata.name

    def teardown_namespace(self):
        """
        Deletes the created namespace
        """
        namespace = self.k8s_client.CoreV1Api().delete_namespace(
            name=self.namespace.metadata.name,
            body=self.k8s_client.V1DeleteOptions()
        )

        return namespace.metadata.name

    # we should always create the deployment with zero replicas as it will be
    # scaled up with the amount of users we want to simulate
    def setup_deployment_ensure_zero_replicas(self, deployment):
        if 'replicas' not in deployment['spec']:
            deployment['spec']['replicas'] = None

        deployment['spec']['replicas'] = 0

        return deployment

    def setup_deployment_fix_restart_policy(self, deployment):
        deployment['spec']['template']['spec']['restartPolicy'] = "Always"

        return deployment

    def setup_deployment_inject_master_endpoint(self, deployment):
        fixed_containers = []

        for container in deployment['spec']['template']['spec']['containers']:
            if 'env' not in container:
                container['env'] = []

            container['env'].append({
                'name': 'LOCUST_HOST',
                'value': self.options.host
            })
            container['env'].append({
                'name': 'LOCUST_MASTER_HOST',
                'value': self.master_host_k8s
            })

            fixed_containers.append(container)

        deployment['spec']['template']['spec']['containers'] = fixed_containers

        return deployment

    def setup_deployment(self, deployment):
        # calls all methods that start with setup_deployment_
        for method in dir(self):
            if method.startswith("setup_deployment_"):
                logger.info("Executing {}".format(method))
                deployment = getattr(self, method)(deployment)

        created_deployment = self.k8s_client.ExtensionsV1beta1Api().create_namespaced_deployment(
            body=deployment,
            namespace=self.namespace,
            pretty=True
        )

        # give some time for the api to process the deployment
        gevent.sleep(5)

        return created_deployment

    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False):
        # everytime we start a new loadtest we need to setup
        # the environment for the slaves on K8s
        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            self.namespace = self.setup_namespace()
            self.slave_deployment = self.setup_deployment(
                self.raw_deployment_definition
            )

        super(K8sLocustRunner, self).start_hatching(*args, **kwargs)


    def spawn_locusts(self, spawn_count=None, stop_timeout=None, wait=False):
        scale = self.k8s_client.ExtensionsV1beta1Api().read_namespaced_deployment_scale(
            name=self.slave_deployment.metadata.name,
            namespace=self.namespace,
            pretty=True
        )

        scale.spec.replicas = spawn_count

        self.k8s_client.ExtensionsV1beta1Api().patch_namespaced_deployment_scale(
            name=self.slave_deployment.metadata.name,
            namespace=self.namespace,
            body=scale
        )

    def kill_locusts(self, kill_count):
        self.spawn_locusts(spawn_count=kill_count)

    def stop(self):
        # once the loadtest is done, we can teardown the read_namespace
        # and everything inside will be gone
        self.teardown_namespace()
