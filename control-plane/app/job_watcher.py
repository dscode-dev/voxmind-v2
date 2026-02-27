from kubernetes import client, config, watch
import os
import logging

logger = logging.getLogger(__name__)


class JobWatcher:

    def __init__(self):
        config.load_incluster_config()
        self.batch_v1 = client.BatchV1Api()
        self.namespace = os.getenv("NAMESPACE", "voxmind-v2")

    def wait_for_completion(self, job_name: str):

        w = watch.Watch()

        for event in w.stream(
            self.batch_v1.list_namespaced_job,
            namespace=self.namespace,
            timeout_seconds=600
        ):
            job = event["object"]

            if job.metadata.name == job_name:

                if job.status.succeeded:
                    w.stop()
                    return "succeeded"

                if job.status.failed:
                    w.stop()
                    return "failed"

        return "timeout"