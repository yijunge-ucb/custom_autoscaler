import logging
import json
import base64


from flask import Flask, request, jsonify
import requests



app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,                     # Set log level (DEBUG, INFO, WARNING, etc.)
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()]      # Log to stdout
)


PROMETHEUS_URL = "http://support-prometheus-server.support.svc.cluster.local"

def query_prometheus(query):
    params = {
        'query': query,
    } 
    response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params=params)
    result = response.json()
    print(result)
    if result["status"] == "success" and result["data"]["result"]:
        return float(result["data"]["result"][0]["value"][1])
    return None

# Convert CPU values
def parse_cpu(cpu_str):
    return int(cpu_str.replace("m", "")) if "m" in cpu_str else int(float(cpu_str) * 1000)

# Convert memory values
def parse_memory(mem_str):
    if mem_str.endswith("Mi"):
        return int(mem_str.replace("Mi", ""))
    elif mem_str.endswith("Gi"):
        return int(float(mem_str.replace("Gi", "")) * 1024)
    return 0

@app.route('/mutate', methods=['POST'])
def mutate():
    """
    Processes of mutating the CPU and memory requests. 
    1. The single user container in a pod is identified by filtering the image name. 
    2. The app querys the Prometheus API for max(95 percentile usage for all the pods in the same namespace).
    3. The app sets the recommended CPU and memory requests to max(95 percentile). 
    """

    admission_review = request.get_json(force=True)
    request_obj = admission_review["request"]
    uid = request_obj['uid']
    pod = request_obj["object"]
    namespace = pod["metadata"]["namespace"]
    pod_name = pod["metadata"]["name"]
    containers = pod["spec"].get("containers", [])

    # Find the container using single user images 
    result = next(
        ((i, c) for i, c in enumerate(containers) if "user-images" in c.get("image", "")),
        (None, None)
    )
    target_index, target_container = result

    recommended_cpu_millicores = 0
    recommended_mem_mebibytes = 0

    if target_container:
        name = target_container.get("name")
        image = target_container.get("image", "")
        resources = target_container.get("resources", {})
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})

        cpu_request_str = requests.get("cpu", "100m") # default 0.1 core if not set
        mem_request_str = requests.get("memory", "256Mi") # default 256 Mi if not set
        cpu_limit_str = limits.get("cpu", "1000m")  # default 1 core if not set
        mem_limit_str = limits.get("memory", "4Gi")  # default 4Gi if not set

        cpu_request = parse_cpu(cpu_request_str)
        cpu_limit = parse_cpu(cpu_limit_str)
        mem_request = parse_memory(mem_request_str)
        mem_limit = parse_memory(mem_limit_str)

        app.logger.info(
            f"Pod: {pod_name}, Container: {name}, Image: {image}, "
            f"CPU Request: {cpu_request}m, Memory Request: {mem_request}Mi, "
            f"CPU Limit: {cpu_limit}m, Memory Limit: {mem_limit}Mi"
        )

        # Prometheus queries
        # 95th percentile CPU usage over a 15mins window
        cpu_query_95 = f'''
            quantile(0.95, avg_over_time(avg by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{namespace}", container!="", container!="mongo", container!="postgres", pod=~"jupyter-.*", cloud_google_com_gke_nodepool=~"user-.*"}}[5m]))[15m:]))
        '''
        
        # 95th percentile memory usage over a 15mins window
        mem_query_95 = f'''
            quantile(0.95, avg_over_time(avg by (pod) (container_memory_usage_bytes{{namespace="{namespace}", container!="", container!="mongo", container!="postgres", pod=~"jupyter-.*", cloud_google_com_gke_nodepool=~"user-.*"}})[15m:]))
        '''
        try:
            cpu_95_percentile = query_prometheus(cpu_query_95)
            mem_95_percentile = query_prometheus(mem_query_95)
        except:
            app.logger.error(f"Prometheus query failed in the namespace {namespace}. ")
            cpu_95_percentile = None
            mem_95_percentile = None

        # Convert to Kubernetes expected units
        if cpu_95_percentile is not None:
            # CPU request can not exceed the CPU limit
            prometheus_cpu = int(cpu_95_percentile * 1000)
            app.logger.info(f"Prometheus 95 percentile CPU is {prometheus_cpu} m.")
            if cpu_limit != 0:
                recommended_cpu_millicores = min(prometheus_cpu, cpu_limit) 
            else:
                recommended_cpu_millicores = prometheus_cpu
        else:
            # If no prometheus data is found, the CPU request is not modified.
            app.logger.info("No Prometheus data is available for modifiying CPU requests. The CPU request will remain unchanged. ")
            recommended_cpu_millicores = cpu_request

        if mem_95_percentile is not None:
            # Memory request can not exceed the memory limit. 
            prometheus_mem = int(mem_95_percentile / 1024 / 1024)
            app.logger.info(f"Prometheus 95 percentile memory is {prometheus_mem} Mi.")
            if mem_limit != 0:
                recommended_mem_mebibytes = min(prometheus_mem, mem_limit)
            else:
                recommended_mem_mebibytes = prometheus_mem
        else:
            # If no prometheus data is found, the memory request is not modified. 
            app.logger.info("No Prometheus data is available for modifiying memory requests. The memory request will remain unchanged. ")
            recommended_mem_mebibytes = mem_request
        
        app.logger.info(f"Recommended CPU Request: {recommended_cpu_millicores}m")
        app.logger.info(f"Recommended Memory Request: {recommended_mem_mebibytes}Mi")
    else:
        app.logger.warning(f"Pod: {pod_name}. No container found using single user images.")


    if target_index is not None:
        patch = [
            {
                "op": "add",
                "path": f"/spec/containers/{target_index}/resources/requests",
                "value": {
                    "cpu": f"{recommended_cpu_millicores}m",
                    "memory": f"{recommended_mem_mebibytes}Mi"
                }
            }
        ]

        admission_response = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid,
                "allowed": True,
                "patchType": "JSONPatch",
                "patch": base64.b64encode(json.dumps(patch).encode()).decode()
            }
        }
    else:
        admission_response = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid,
                "allowed": True
            }
        }

    return jsonify(admission_response)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=443, ssl_context=('/certs/tls.crt', '/certs/tls.key'))



