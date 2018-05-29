from os.path import expanduser, join
from datetime import datetime
import random
import time
import ast
import json
import threading
import requests

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import numpy as np
import pandas as pd

from google.cloud import pubsub_v1

class NeuromniSimulator():
  """Simulate multiple clients making simultaneous requests of the master.

  This class sets up multiple clients that will execute reads and writes to
  simulate a load on the master and graph server. Each client will record
  when it issues a particular request. We will resever one separate client
  that will record when it receives an update from the master server when any 
  request has been executed.

  Attributes:
    supervoxels: [int] list of supervoxel IDs to split across the clients
    num_clients: number of clients issuing requests to the master server.
    num_readers: number of clients that will only issue read
      requests to the master server.
    num_writers: number of clients that will issue both read and write 
      requests to the master server.
    read_frequency: time in seconds between read requests for each client.
    write_frequency: time in seconds between write requests for each client.
    runtime: (int) seconds for which the simulation will run
  """
  
  def __init__(self, receiver, supervoxels, num_readers=1, num_writers=1, read_frequency=5, 
                      write_frequency=5, runtime=120, dir=expanduser('~')):
    self.num_readers = num_readers
    self.num_writers = num_writers
    self.read_frequency = read_frequency
    self.write_frequency = write_frequency
    self.runtime = runtime
    self.clients = []
    self.receiver = receiver
    self.dir = dir
    self.supervoxels = supervoxels
    self.init_clients()

  def init_clients(self):
    for i, sv in enumerate(np.array_split(self.supervoxels, self.num_writers)):
      c = Client(i, sv, read_frequency=self.read_frequency, 
                    write_frequency=self.write_frequency, runtime=self.runtime)
      self.clients.append(c)
    if self.num_readers > 0:
      for i, sv in enumerate(np.array_split(self.supervoxels, self.num_readers)):
        c = Client(i+self.num_writers, sv, read_frequency=self.read_frequency, 
                                          write_frequency=0, runtime=self.runtime)
        self.clients.append(c)

  def run(self):
    self.receiver.reset_log()
    for c in self.clients:
      c.start()
    for c in self.clients:
      c.join()
  
  def get_logs(self):
    logs = []
    for c in self.clients:
      logs.append(c.export_log())
    c_df = pd.concat(logs)
    r_df = self.receiver.export_log()
    # return pd.merge(c_df, r_df, how='outer', on=['op','v1','v2'])
    return c_df, r_df

  def save_logs(self):
    c_df, r_df = self.get_logs()
    c_fn = 'NeuromniSimulator_{0}_readers_{1}_writers_{2}_seconds_clients'.format(self.num_readers, self.num_writers, self.runtime)
    c_path = join(self.dir, c_fn)
    c_df.to_csv(c_path)
    r_fn = 'NeuromniSimulator_{0}_readers_{1}_writers_{2}_seconds_receiver'.format(self.num_readers, self.num_writers, self.runtime)
    r_path = join(self.dir, r_fn)
    r_df.to_csv(r_path)

class Client(threading.Thread):
  """Single client simulator.

  Attributes:
    id: (int) unique identifier for the client
    supervoxels: [int] list of supervoxel IDs to execute actions between
    bbox: chunk from which to request the subgraph
    read_frequency: time in seconds between read requests for each client.
    write_frequency: time in seconds between write requests for each client.
    runtime: (int) seconds for which the simulation will run 
    subgraphs: dict of subgraphs (list of vertex pairs) indexed by root ID
    edge: tuple of ints, that represent a saved edge for merging, then splitting
    log: list of edits made   
  """

  def __init__(self, id, supervoxels, bbox=[[0,0,0],[10,10,10]],
                        read_frequency=5, write_frequency=5, runtime=60):
    threading.Thread.__init__(self)
    self.id = id
    self.read_frequency = read_frequency
    self.write_frequency = write_frequency
    self.runtime = runtime
    self.bbox = bbox
    self.supervoxels = supervoxels 
    self.subgraphs = {}
    self.edge = None
    self.log = []

  def read(self, supervoxel):
    root = self.get_root(supervoxel)
    self.get_subgraph(root)

  def get_root(self, supervoxel):
    op = '{0}/root'.format(supervoxel)
    response = self.request(op, post=False)
    return int(response['id'])

  def get_subgraph(self, root):
    op = 'subgraph'
    data = {"root_id": root, "bbox": self.bbox}
    response = self.request(op, data)
    self.subgraphs[root] = response['edges']

  def split(self):
    op = 'split'
    data = {'edge': self.edge}
    response = self.request(op, data)

  def merge(self):
    op = 'merge'
    data = {'edge': self.edge}
    response = self.request(op, data)

  def request(self, op, data_dict={}, post=True):
    print('{0}; {1}'.format(self.id, (op, data_dict)))
    if post:
      url = 'https://35.231.236.20:4000/1.0/graph/{0}/'.format(op)
      data = json.dumps(data_dict)
      headers = {'Content-Type': 'application/json'}
      request_time = datetime.now().timestamp()
      response = requests.post(url, verify=False, data=data, headers=headers)
    else:
      url = 'https://35.231.236.20:4000/1.0/segment/{0}/'.format(op)
      request_time = datetime.now().timestamp()
      response = requests.get(url, verify=False)
      data_dict = op
      op = 'root'
      # dummy response data
      # response = {'time_server_start': 0,
      #             'time_graph_start':0,
      #             'time_graph_end':0,
      #             'edges': np.random.randint(200,300,(3,2)).tolist(),
      #             'id': random.randint(1,100)}
    response_time = datetime.now().timestamp()
    response = response.json()
    edge = [0,0]
    if op in ['merge', 'split']:
      edge = data_dict['edge']
    elif op == 'subgraph':
      edge = [data_dict['root_id'], 0]
    elif op == 'root':
      edge = [data_dict.split('/')[0], 0]
    self.update_log(op, edge, response, request_time, response_time)
    return response

  def update_log(self, op, edge, response, request_time, response_time):
    master_start = datetime_to_float(response['time_server_start'])
    graph_start = datetime_to_float(response['time_graph_start'])
    graph_stop = datetime_to_float(response['time_graph_end'])
    entry = [self.id, op, edge[0], edge[1], request_time, master_start, 
                                      graph_start, graph_stop, response_time]
    self.log.append(entry)

  def export_log(self):
    c = ['id','op','v1','v2','client_request','master_start', 'graph_start', 
                                                'graph_stop','client_receipt']
    return pd.DataFrame(self.log, columns=c)

  def select_edge(self, vertices1, vertices2):
    v1 = np.random.choice(vertices1)
    v2 = v1
    while v2 == v1: 
      v2 = np.random.choice(vertices2)
    self.edge = [int(v1), int(v2)]
    print('{0}; select_edge: {1}'.format(self.id, self.edge))

  def get_vertices(self, subgraph):
    return np.unique([v for edge in subgraph for v in edge])

  def load_subgraphs(self, n=2):
    self.subgraphs = {}
    supervoxels = np.random.choice(self.supervoxels, n, replace=False)
    for sv in supervoxels:
      self.read(sv)

  def select_edge_to_merge(self):
    vertices1 = self.get_vertices(list(self.subgraphs.values())[0])
    vertices2 = self.get_vertices(list(self.subgraphs.values())[1])
    self.select_edge(vertices1, vertices2)

  def select_edge_to_split(self):
    vertices = self.get_vertices(list(self.subgraphs.values())[0])
    self.select_edge(vertices, vertices)

  def run(self):
    # stagger start randomly
    time.sleep(random.random()*10)
    print('{0}; starting'.format(self.id))
    # start simulation runtime    
    start_time = time.time()
    while time.time() - start_time < self.runtime:
      # always read in the subgraphs for two supervoxels
      # we'll then merge the two subgraphs, then we'll split them
      if self.read_frequency > 0:
        time.sleep(self.read_frequency)
        self.load_subgraphs()
      if self.write_frequency > 0:       
        time.sleep(self.write_frequency)
        # if we've selected two supervoxels with the same root, then skip merge
        if len(self.subgraphs.keys()) == 1:
          print('{0}; same root, selecting edge to split'.format(self.id))
          self.select_edge_to_split()
        else: 
          print('{0}; different roots, select_edge_to_merge'.format(self.id))
          self.select_edge_to_merge()
          self.merge()
          time.sleep(self.write_frequency)
        self.split()

class Receiver():
  """Simulator of a no-op client that's listening to the pub/sub channel
  """

  def __init__(self, project, subscription_name):
    self.project = project
    self.subscription_name = subscription_name
    self.log = []

  def reset_log(self):
    self.log = []

  def export_log(self):
    log = []
    for entry in self.log:
      m, timestamp = entry
      data = m.data.decode('utf-8').split(' ')
      op = data[0]
      edge = []
      for d in data[1:]:
        for x in d.split('['):
          for y in x.split(']'):
            if len(y) > 1:
              edge.append(int(y))
      # log.append([op, data, timestamp])    
      log.append([op, edge[0], edge[1], timestamp])    
    return pd.DataFrame(log, columns=['op','v1','v2','receiver_receipt'])

  def update_log(self, m, timestamp):
    self.log.append([m, timestamp])

  def start(self):
      """Receives messages from a pull subscription."""
      subscriber = pubsub_v1.SubscriberClient()
      subscription_path = subscriber.subscription_path(
          self.project, self.subscription_name)

      def callback(message):
          print('Received message: {}'.format(message))
          self.update_log(message, datetime.now().timestamp())
          message.ack()

      print('Start listening for messages on {}'.format(subscription_path))
      future = subscriber.subscribe(subscription_path, callback=callback)

def datetime_to_float(dt):
  return datetime.strptime(dt, '%Y-%m-%d %H:%M:%S.%f').timestamp()



def project_simulations():
  axons = np.array([1073872745,1073877767,1342259501,1342265089,1342265248,
                    1342282778,1610757848,1610757930,1610758328,1610763716,
                    1879163963,1879172639,2147608833,2147609338,2147613253,
                    2147629746,2416061756,2416091572,2416091701,268546551,
                    268566391,268567659,268574828,2952856782,3221345314,
                    3221347192,3221356706,3221362581,3221364254,3489761217,
                    3489764604,3489779429,3758208023,3758208753,4026600005,
                    4026615577,4026627905,536951520,536956914,536960992,
                    536965629,536970379,60231,63927,70458,805453908,
                    805459674,805475912,805481910])
  mixed_supervoxels = np.array([1073866276,1073884365,1610758466,1610758483,
                        1610758848,1879167187,1879172874,2684416363,
                        268583651,3758198161,3758212775,4026604416,4026618602])
  # low load
  n = 17
  k = 2
  num_low_readers = [i for i in range(1,n,k)]
  num_low_writers = [1 for i in range(1,n,k)]

  # high load
  num_high_readers = [0 for i in range(2,n,k)]
  num_high_writers = [i for i in range(2,n,k)]  

  scenarios = zip(num_high_readers + num_low_readers, 
                                    num_high_writers + num_low_writers)
  receiver = Receiver('neuromancer-seung-import', 'MySub')
  receiver.start()
  for num_readers, num_writers in scenarios:
    print((num_readers, num_writers))
    ns = NeuromniSimulator(receiver, axons, num_readers=num_readers, 
                  num_writers=num_writers, dir='data/axons')
    ns.run()
    time.sleep(30)
    ns.save_logs()

if __name__ == '__main__':
  project_simulations()