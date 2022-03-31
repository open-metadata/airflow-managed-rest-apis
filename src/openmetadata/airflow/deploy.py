#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import json
import logging
import os
from typing import Dict

from airflow import settings
from airflow.models import DagBag, DagModel
from airflow.models.serialized_dag import SerializedDagModel
from jinja2 import Template
from metadata.generated.schema.operations.pipelines.airflowPipeline import (
    AirflowPipeline,
)

from openmetadata.api.response import ApiResponse
from openmetadata.api.rest_api import (
    AIRFLOW_DAGS_FOLDER,
    DAG_GENERATED_CONFIGS,
    DAG_RUNNER_TEMPLATE,
    HOSTNAME,
)
from openmetadata.api.utils import import_path


class DagDeployer:
    """
    Helper class to store DAG config
    and deploy it to Airflow
    """

    def __init__(self, airflow_pipeline: AirflowPipeline, dag_bag: DagBag):

        logging.info(
            f"Received the following Airflow Configuration: {airflow_pipeline.airflowConfig}"
        )

        self.airflow_pipeline = airflow_pipeline
        self.dag_bag = dag_bag

    def store_airflow_pipeline_config(self) -> Dict[str, str]:
        """
        Validate if we need to force deploy the DAG.

        Store the airflow pipeline config in a JSON file and
        return the path.
        """
        dag_config_file_path = os.path.join(
            DAG_GENERATED_CONFIGS, f"{self.airflow_pipeline.name}.json"
        )
        logging.info(dag_config_file_path)
        # Check if the file already exists.
        if (
            os.path.isfile(dag_config_file_path)
            and not self.airflow_pipeline.airflowConfig.forceDeploy
        ):
            logging.warning("File to upload already exists and forceDeploy is false")
            return ApiResponse.bad_request(
                "The file '"
                + dag_config_file_path
                + "' already exists on host '"
                + HOSTNAME
            )

        logging.info("Saving file to '" + dag_config_file_path + "'")
        with open(dag_config_file_path, "w") as outfile:
            json.dump(self.airflow_pipeline.dict(), outfile)

        return {"workflow_config_file": dag_config_file_path}

    def store_and_validate_dag_file(self, dag_runner_config: Dict[str, str]) -> str:
        """
        Stores the Python file generating the DAG and returns
        the rendered strings
        """

        dag_py_file = os.path.join(
            AIRFLOW_DAGS_FOLDER, f"{self.airflow_pipeline.name}.py"
        )

        with open(DAG_RUNNER_TEMPLATE, "r") as f:
            template = Template(f.read())
        rendered_dag = template.render(dag_runner_config)
        with open(dag_py_file, "w") as f:
            f.write(rendered_dag)

        try:
            dag_file = import_path(dag_py_file)
        except Exception as e:
            warning = "Failed to import dag_file"
            logging.error(e)
            logging.warning(warning)
            return ApiResponse.server_error("Failed to import {}".format(dag_py_file))

        try:
            if dag_file is None:
                warning = "Failed to get dag"
                logging.warning(warning)
                return ApiResponse.server_error(
                    "DAG File [{}] has been uploaded".format(dag_file)
                )
        except Exception:
            warning = "Failed to get dag from dag_file"
            logging.warning(warning)
            return ApiResponse.server_error(
                "Failed to get dag from DAG File [{}]".format(dag_file)
            )

        return dag_py_file

    def refresh_session_dag(self, dag_py_file: str):
        """
        Get the stored python DAG file and update the
        Airflow DagBag and sync it to the db
        """

        # Refresh dag into session
        session = settings.Session()
        try:
            logging.info("dagbag size {}".format(self.dag_bag.size()))
            found_dags = self.dag_bag.process_file(dag_py_file)
            logging.info("processed dags {}".format(found_dags))
            dag = self.dag_bag.get_dag(self.airflow_pipeline.name, session=session)
            SerializedDagModel.write_dag(dag)
            dag.sync_to_db(session=session)
            dag_model = (
                session.query(DagModel)
                .filter(DagModel.dag_id == self.airflow_pipeline.name)
                .first()
            )
            logging.info("dag_model:" + str(dag_model))
            dag_model.set_is_paused(
                is_paused=self.airflow_pipeline.airflowConfig.pausePipeline
            )
            return ApiResponse.success(
                {
                    "message": "Workflow [{}] has been created".format(
                        self.airflow_pipeline.name
                    )
                }
            )
        except Exception as e:
            logging.info(f"Failed to serialize the dag {e}")
            return ApiResponse.server_error(
                {
                    "message": "Workflow [{}] failed to deploy due to [{}]".format(
                        self.airflow_pipeline.name, e
                    )
                }
            )

    def deploy(self):
        """
        Run all methods to deploy the DAG
        """
        dag_runner_config = self.store_airflow_pipeline_config()
        dag_py_file = self.store_and_validate_dag_file(dag_runner_config)
        self.refresh_session_dag(dag_py_file)
