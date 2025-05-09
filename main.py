from flask import Flask, request, jsonify
import base64
import json
import sys

app = Flask(__name__)

@app.route('/mutate', methods=['POST'])
def mutate():
    try:
        req = request.get_json(force=True)
        print("Incoming request JSON:", json.dumps(req), file=sys.stderr)
    except Exception as e:
        print("Failed to parse request:", str(e), file=sys.stderr)
        return "Bad request", 400
    uid = req['request']['uid']

    patch = [
        {
            "op": "add",
            "path": "/spec/containers/0/resources",
            "value": {
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"}
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

    return jsonify(admission_response)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=443, ssl_context=('/certs/tls.crt', '/certs/tls.key'))


