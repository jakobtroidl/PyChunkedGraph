from flask import Flask, jsonify, Response, request
from threading import Lock
from flask_cors import CORS
import sys
#sys.path.insert(0, '/home/zashwood/PyChunkedGraph/src/pychunkedgraph') #Include Sven's pychunkedgraph code
sys.path.insert(0, '/usr/people/zashwood/Documents/PyChunkedGraph/src/pychunkedgraph') #Include Sven's pychunkedgraph code
import chunkedgraph #Import chunkedgraph script 
import numpy as np
import time
import redis
# curl -i http://localhost:4000/1.0/segment/537753696/root/
# SPLIT:
  #  curl -X POST -H "Content-Type: application/json" -d  '{"edge":"537753696, 537544567"}' http://localhost:4000/1.0/graph/split/
# MERGE:
    #curl -X POST -H "Content-Type: application/json" -d '{"edge":"537753696, 537544567"}' http://localhost:4000/1.0/graph/merge/
# GET SUBGRAPH 
   # curl -X POST -H "Content-Type: application/json" -d '{"root_id":"432345564227567621","bbox":"0, 0, 0, 10, 10, 10"}' http://localhost:4000/1.0/graph/subgraph/

app = Flask(__name__)
CORS(app)

redis_conn = redis.StrictRedis(
   host="redis", port=6379, charset="utf-8", decode_responses=True)

@app.route('/')
def index():
    return ""

@app.route('/1.0/segment/<atomic_id>/root', methods=['GET'])
def handle_root(atomic_id):
	#Read and write to redis
    redis_conn.get("/id/%s"%str(atomic_id),"busy")
    redis_conn.set("/id/%s"%str(atomic_id),"busy")

    root_id = cg.get_root(int(atomic_id))
    print(root_id)
    return jsonify({"id": str(root_id)})

@app.route('/1.0/graph/merge/', methods=['POST'])
def handle_merge():
    # Collect edges from json:
    if 'edge' in request.get_json():
        edge = request.get_json()['edge']
    # Obtain edges from request dictionary, and convert to numpy array with uint64s
        edge = np.fromstring(edge, sep = ',', dtype = np.uint64)
        try: 
        	#Check if either of the root_IDs are being processed by any of the threads currently: 
        	# Get root IDs for both supervoxels:
        	root1 = cg.get_root(int(edge[0]))
        	root2 = cg.get_root(int(edge[1]))
        	print(root1)
        	# Furthermore, get historical agglomeration IDs for these
        	historical1 = cg.read_agglomeration_id_history(root1)
        	historical2 = cg.read_agglomeration_id_history(root2)
        	all_historical_ids = np.append(historical1, historical2)
        	print(all_historical_ids)
        	# Now check if any of the historical IDs are being processed in redis DB: (inefficient - will not scale to large #s of historical IDs)
        	for i, historic_id in enumerate(all_historical_ids):
        		redis_conn.set(str(historic_id),"busy")
        		print(redis_conn.get(str(historic_id)))
    		# If not, proceed with request and append current ID to redis
    		#redis_conn.get("/id/%s"%str(atomic_id),"busy")
    		#redis_conn.set("/id/%s"%str(atomic_id),"busy")	
        	#out = cg.add_edge(edge)
        	out = 'Happy'
        	# remove element from redis since we have now performed update to graph
        except:
        	out = 'NaN'
        	# remove
        return jsonify({"new_root_id": str(out)})
    else: 
    	return '', 400

@app.route('/1.0/graph/split/', methods=['POST'])
def handle_split():
    #Read and write to redis
    #redis_conn.get("/id/%s"%str(atomic_id),"busy")
    #redis_conn.set("/id/%s"%str(atomic_id),"busy")
    # Collect edges from json:
    if 'edge' in request.get_json():
        edge = request.get_json()['edge']
    # Obtain edges from request dictionary, and convert to numpy array with uint64s
        edge = np.fromstring(edge, sep = ',', dtype = np.uint64)
        try: 
        	out = cg.remove_edge(edge)
        except:
        	out = 'NaN'
        return jsonify({"new_root_ids": str(out)})
    else: 
    	return '', 400

@app.route('/1.0/graph/subgraph/', methods=['POST'])
def get_subgraph():
    #Read and write to redis
    #redis_conn.get("/id/%s"%str(atomic_id),"busy")
    #redis_conn.set("/id/%s"%str(atomic_id),"busy")
    # Collect edges from json:
    if 'root_id' in request.get_json() and 'bbox' in request.get_json():
    	root_id = int(request.get_json()['root_id'])
    	bounding_box  = np.reshape(np.array(request.get_json()['bbox'].split(','), dtype = int), (2,3))
    	try:
    		edges, affinities = cg.get_subgraph(root_id, bounding_box)
    	except:
    		edges, affinities = 'NaN', 'NaN'
    	return jsonify({"edges":edges.tolist(), 'affinities':affinities.tolist()})	
    else: 
    	return '', 400


if __name__ == '__main__':
    # Initialize chunkedgraph:
    cg = chunkedgraph.ChunkedGraph(dev_mode=False)
    app.run(host = 'localhost', port = 4000, debug = True, threaded=True)