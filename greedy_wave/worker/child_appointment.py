"""
 * Copyright (c) 2018, Autonomous Networks Research Group. All rights reserved.
 *     contributors: 
 *      Pranav Sakulkar
 *      Jiatong Wang
 *      Pradipta Ghosh
 *      Bhaskar Krishnamachari
 *     Read license file in main directory for more details  
"""

# -*- coding: utf-8 -*-

import json
import re
import threading
import time
import os
import sys
import urllib
from urllib import parse
import shutil
import _thread
from flask import Flask, request
import requests
from pymongo import MongoClient

app = Flask(__name__)

'''
'''

# Get ALL node info
node_count = 0
nodes = {}
ip_to_node_name = {}
docker_ip_to_node_name = {}

for node_name, node_ip in zip(os.environ['ALL_NODES'].split(":"), os.environ['ALL_NODES_IPS'].split(":")):
    if node_name == "":
        continue
    nodes[node_name] = node_ip + ":48080"
    ip_to_node_name[node_ip] = node_name
    node_count += 1

master_host = os.environ['HOME_IP'] + ":48080"
print("Nodes", nodes)
print("os.en:", os.environ['PROFILER'])

#
threshold = 15
resource_data = {}
is_resource_data_ready = False
network_profile_data = {}
is_network_profile_data_ready = False

#
node_id = -1
node_name = ""
debug = True

# control relations between tasks
control_relation = {}

local_children = "local/local_children.txt"
local_mapping = "local/local_mapping.txt"
local_responsibility = "local/task_responsibility"

# lock for sync file operation
lock = threading.Lock()
kill_flag = False


@app.route('/assign_task')
def assign_task():
    try:
        task_name = request.args.get('task_name')
        write_file(local_responsibility + "/" + task_name, [], "w+")
        return "ok"
    except Exception:
        return "not ok"


@app.route('/kill_thread')
def kill_thread():
    global kill_flag
    kill_flag = True
    return "ok"


def init_folder():
    print("Trying to initialize folders here")
    try:
        if not os.path.exists("./local"):
            os.mkdir("./local")

        if not os.path.exists(local_children):
            write_file(local_children, [], "w+")

        if not os.path.exists(local_mapping):
            write_file(local_mapping, [], "w+")

        if not os.path.exists(local_responsibility):
            os.mkdir(local_responsibility)
        return "ok"
    except Exception:
        print("Init folder fialed: Why??")
        return "not ok"


@app.route('/recv_control')
def recv_control():
    try:
        control = request.args.get('control')
        items = re.split(r'#', control)

        to_be_write = []
        for _, item in enumerate(items):
            to_be_write.append(item.replace("__", "\t"))
            tmp = re.split(r"__", item)
            key = tmp[0]
            del tmp[0]
            control_relation[key] = tmp

        if not os.path.exists("./DAG"):
            os.mkdir("./DAG")

        write_file("DAG/parent_controller.txt", to_be_write, "a+")
    except Exception:
        return "not ok"
    return "ok"


def assign_task_to_remote(assigned_node, task_name):
    try:
        url = "http://" + nodes[assigned_node] + "/assign_task"
        params = {'task_name': task_name}
        params = parse.urlencode(params)
        req = urllib.request.Request(url='%s%s%s' % (url, '?', params))
        res = urllib.request.urlopen(req)
        res = res.read()
        res = res.decode('utf-8')
    except Exception:
        return "not ok"
    return res


def call_send_mapping(mapping, node):
    try:
        url = "http://" + master_host + "/recv_mapping"
        params = {'mapping': mapping, "node": node}
        params = parse.urlencode(params)
        req = urllib.request.Request(url='%s%s%s' % (url, '?', params))
        res = urllib.request.urlopen(req)
        res = res.read()
        res = res.decode('utf-8')
    except Exception as e:
        return "not ok"
    return res


# thread to watch directory: local/task_responsibility
def watcher():
    pre_time = time.time()

    tmp_mapping = ""
    while True:
        if kill_flag:
            break

        if not os.path.exists(local_responsibility):
            time.sleep(1)
            continue

        tasks = scan_dir(local_responsibility)
        output("Find tasks under task_responsibility: " + json.dumps(tasks))
        for _, task in enumerate(tasks):
            if task in control_relation.keys():
                controlled = control_relation[task]
                for _, t in enumerate(controlled):
                    todo_task = t + "\tTODO"
                    write_file(local_children, [todo_task], "a+")
                    output("Write content to local_children.txt: " + todo_task)

                write_file(local_mapping, [task], "a+")
                if tmp_mapping != "":
                    tmp_mapping = tmp_mapping + "#"
                tmp_mapping = tmp_mapping + task

                output("Write content to local_mapping.txt: " + task)
            else:
                write_file(local_mapping, [task], "a+")
                if tmp_mapping != "":
                    tmp_mapping = tmp_mapping + "#"
                tmp_mapping = tmp_mapping + task

                output("Write content to local_mapping.txt: " + task)

        shutil.rmtree(local_responsibility)
        os.mkdir(local_responsibility)

        if time.time() - pre_time >= 60:
            if tmp_mapping != "":
                res = call_send_mapping(tmp_mapping, node_name)
                if res != "ok":
                    output("Send mapping content to master failed");
                else:
                    tmp_mapping = ""

            pre_time = time.time()

        time.sleep(10)


# thread to assign todo task to nodes
def distribute():
    done_dict = set()
    start = int(time.time())

    # wait for network_profile_data and resource_data be ready
    while True:
        if is_network_profile_data_ready and is_resource_data_ready:
            break
        else:
            time.sleep(100)
            end = int(time.time())
            if end - start >= 1800:
                break

    while True:
        if kill_flag:
            break

        if not os.path.exists(local_children):
            time.sleep(1)
            continue

        lines = read_file(local_children)
        for line in lines:
            line = line.strip()
            if "TODO" in line:
                output("Find todo item: " + line)

                items = re.split(r'\t+', line)
                if items[0] in done_dict:
                    print(items[0], "already assigned")
                    continue

                assign_to_node = get_most_suitable_node(len(items[0]))
                if not assign_to_node:
                    print("No suitable node found for assigning task: ", items[0])
                    continue

                print("Trying to assign", items[0], "to", assign_to_node)
                status = assign_task_to_remote(assign_to_node, items[0])
                if status == "ok":
                    assign_info = {
                        'node_id': assign_to_node,
                        'task': items[0],
                        'time': time.time()
                    }
                    output(str(assign_info))

                    summary_info = 'node_name: %s, task: %s, time: %s' % \
                                   (assign_to_node, items[0], str(time.time()))
                    send_task_assign_info_to_master(summary_info)

                    done_dict.add(items[0])
                else:
                    output("Assign " + items[0] + " to " + assign_to_node + " failed")

        time.sleep(10)

#send summary of task assignment info to master node and print
def send_task_assign_info_to_master(info):
    try:
        tmp_home_ip = os.environ['HOME_IP']
        url = "http://" + tmp_home_ip + ":" + "48080" + "/recv_task_assign_info?assign=" + info
        resp = requests.get(url).text
        if resp == "ok":
            output('Send task assign info to master succeeded')
            return
    except Exception as e:
        print("Send task assign info to master, details: " + str(e))

    output('Send task assign info to master failed')

#Calculate network delay + resource delay
def get_most_suitable_node(size):
    weight_network = 1
    weight_cpu = 1
    weight_memory = 1

    valid_nodes = []
    all_nodes = []

    min_value = sys.maxsize
    for tmp_node_name in network_profile_data:
        data = network_profile_data[tmp_node_name]
        a = data['a']
        b = data['b']
        c = data['c']

        tmp = a * size * size + b * size + c
        if tmp < min_value:
            min_value = tmp

        all_nodes.append({'node_name': tmp_node_name, 'delay': tmp, "node_ip": data['ip']})

    print(all_nodes)

    # get all the nodes that satisfy: time < tmin * threshold
    for _, item in enumerate(all_nodes):
        if item['delay'] < min_value * threshold:
            valid_nodes.append(item)

    min_value = sys.maxsize
    result_node_name = ''
    for _, item in enumerate(valid_nodes):
        tmp_node_name = item['node_name']
        tmp_value = item['delay']

        if (tmp_node_name not in resource_data.keys()) or (tmp_node_name == 'node77'):
            tmp_cpu = 10000
            tmp_memory = 10000
        else:
            tmp_resource_data = resource_data[tmp_node_name]
            tmp_cpu = tmp_resource_data['cpu']
            tmp_memory = tmp_resource_data['memory']

        if weight_network*tmp_value + weight_cpu*tmp_cpu + weight_memory*tmp_memory < min_value:
            min_value = weight_network*tmp_value + weight_cpu*tmp_cpu + weight_memory*tmp_memory
            result_node_name = tmp_node_name

    if not result_node_name:
        min_value = sys.maxsize
        for tmp_node_name in resource_data:
            tmp_resource_data = resource_data[tmp_node_name]
            tmp_cpu = tmp_resource_data['cpu']
            tmp_memory = tmp_resource_data['memory']
            if weight_cpu*tmp_cpu + weight_memory*tmp_memory < min_value:
                min_value = weight_cpu*tmp_cpu + weight_memory*tmp_memory
                result_node_name = tmp_node_name

    if result_node_name:
        # del network_profile_data[result_node_name]
        network_profile_data[result_node_name]['c'] = 100000
            # resource_data[result_node_name]['cpu'] = 100000

    return result_node_name


def scan_dir(directory):
    tasks = []
    for file_name in os.listdir(directory):
        tasks.append(file_name)
    return tasks


def create_file():
    pass


def write_file(file_name, content, mode):
    lock.acquire()
    file = open(file_name, mode)
    for line in content:
        file.write(line + "\n")
    file.close()
    lock.release()


# get all lines in a file
def read_file(file_name):
    lock.acquire()
    file_contents = []
    file = open(file_name)
    line = file.readline()
    while line:
        file_contents.append(line)
        line = file.readline()
    file.close()
    lock.release()
    return file_contents


def output(msg):
    if debug:
        print(msg)


# @app.route('/')
# def hello_world():
#     return 'Hello World!'


def get_resource_data():
    print("Starting resource profile collection thread")
    # Requsting resource profiler data using flask for its corresponding profiler node
    RP_PORT = 6100
    st = int(time.time())
    while True:
        time.sleep(60)
        try:
            r = requests.get("http://" + os.environ['PROFILER'] + ":" + str(RP_PORT) + "/all")
            result = r.json()
            print(result)
            if len(result) != 0:
                break
            else:
                et = int(time.time())
                if et -st >= 180:
                    break
        except Exception as e:
            print("Resource request failed. Will try again, details: " + str(e))
            break
    global resource_data
    resource_data = result

    global is_resource_data_ready
    is_resource_data_ready = True

    print("Got profiler data from http://" + os.environ['PROFILER'] + ":" + str(RP_PORT))
    print("Resource profiles: ", json.dumps(result))


def get_network_profile_data():
    # Collect the network profile from local MongoDB peer
    print('Collecting Netowrk Monitoring Data from MongoDB')
    NP_PORT = 6200
    start_ts = int(time.time())
    while True:
        try:
            client_mongo = MongoClient('mongodb://' + os.environ['PROFILER'] + ':' + str(NP_PORT) + '/')
            db = client_mongo.droplet_network_profiler
            table_name = os.environ['PROFILER']
            # print(db[table_name])

            #get mongoDB collections
            collections = db.collection_names(include_system_collections=False)
            print(collections)

            num_db = len(nodes)-1
            num_rows = db[table_name].count()

            global network_profile_data
            network_profile_data.clear()

            #wait till network data load
            while num_rows < num_db :
                print("Network profiler info not yet loaded into mongoDB")
                time.sleep(360)
                num_rows = db[table_name].count()

            #Parse parameters metrix from network profiler
            for item in db[table_name].find():
                if 'Parameters' not in item or 'Destination[IP]' not in item:
                    continue
                destination_ip = item['Destination[IP]']
                if destination_ip in docker_ip_to_node_name:
                    tmp_node_name = docker_ip_to_node_name[destination_ip]
                    params = item['Parameters']
                    param_items = re.split(r'\s+', params)
                    network_profile_data[tmp_node_name] = {'a': float(param_items[0]), 'b': float(param_items[1]),
                                                           'c': float(param_items[2]), 'ip': destination_ip}
            if len(network_profile_data) > 0:
                break
            else:
                tmp_ts = int(time.time())
                if tmp_ts - start_ts >= 600:
                    break

        except Exception as e:
            print('Get network profile data error, details: ' + str(e))
            end_time = int(time.time())
            time.sleep(10)
            if end_time - start_ts >=600:
                break
            

    global is_network_profile_data_ready
    is_network_profile_data_ready = True


# used to send (node_name - profiler ip) mapping to master node
def send_node_name2docker_ip_to_master():
    flag = False
    try_times = 0
    while True:
        try:
            if try_times >= 3:
                break

            tmp_home_ip = os.environ['HOME_IP']
            tmp_docker_ip = os.environ['PROFILER']
            tmp_node_name = os.environ['SELF_NAME']

            mapping = tmp_node_name + ":" + tmp_docker_ip
            url = "http://" + tmp_home_ip + ":" + "48080" + "/recv_node_name2docker_ip?mapping=" + mapping
            resp = requests.get(url)
            text = resp.text
            if text == 'ok':
                flag = True
                break
            else:
                try_times += 1
        except Exception as e:
            try_times += 1
            print('Send node_name to docker_ip mapping to master error, details: ' + str(e))

    if not flag:
        print("Send node_name to docker_ip mapping to master failed")
    else:
        print("Send node_name to docker_ip mapping to master succeed")


# get all the (profiler ip - node name) mapping from master node
def get_node_name2docker_ip_mapping_from_master():
    flag = False
    try_times = 0
    while True:
        try:
            if try_times >= 3:
                break

            tmp_home_ip = os.environ['HOME_IP']
            url = "http://" + tmp_home_ip + ":" + "48080" + "/send_node_name2docker_ip"
            resp = requests.get(url)
            text = resp.text

            global docker_ip_to_node_name
            docker_ip_to_node_name = json.loads(text)
            if len(docker_ip_to_node_name) > 0:
                flag = True
                break
            else:
                try_times += 1
        except Exception as e:
            try_times += 1
            print('Recv node name to docker ip mappings from master error, details: ' + str(e))

    if not flag:
        print("Recv node name to docker ip mappings from master failed")
    else:
        print("Recv node name to docker ip mappings from master succeed")


def sync_docker_ip2node_name_info():
    send_node_name2docker_ip_to_master()
    for i in range(10):
        print("Waiting for all the other nodes report docker ip to node name mapping to master")
        time.sleep(10)
    get_node_name2docker_ip_mapping_from_master()

    


if __name__ == '__main__':
    node_name = os.environ['SELF_NAME']
    node_id = int(node_name.split("e")[-1])

    node_port = sys.argv[1]

    print("Node name:", node_name, "and id", node_id)
    print("Starting the main thread on port", node_port)

    while init_folder() != "ok":  # Initialize the local folers
        pass

    _thread.start_new_thread(sync_docker_ip2node_name_info, ())

    # Get resource data
    _thread.start_new_thread(get_resource_data, ())

    # Get network profile data
    _thread.start_new_thread(get_network_profile_data, ())

    _thread.start_new_thread(watcher, ())
    _thread.start_new_thread(distribute, ())

    app.run(host='0.0.0.0', port=int(node_port))
