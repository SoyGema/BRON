import logging
from typing import List, Any
import argparse
import os
import sys
import json

import requests
import rdflib
import arango

from utils.mitigation_utils import import_into_arango, update_graph_in_graph_db
from mitigations.query_d3fend import (
    find_mitigation_label,
    find_techniques_from_mitigations,
    find_mitigations,
)
from graph_db.bron_arango import (
    DB,
    create_graph,
    get_edge_collection_name,
    get_schema,
    validate_entry,
)
from graph_db.query_graph_db import get_technique_id_from_id


D3FEND_URL = "https://d3fend.mitre.org/resources/"
D3FEND_TECHNIQUE_TREE = "https://d3fend.mitre.org/ontologies/d3fend.csv"
D3FEND_ONTOLOGY_JSON = "https://d3fend.mitre.org/ontologies/d3fend.json"
D3FEND_ONTOLOGY_OWL = "https://d3fend.mitre.org/ontologies/d3fend.owl"
D3FEND_ONTOLOGY_TTL = "https://d3fend.mitre.org/ontologies/d3fend.ttl"
OUT_DIR = "data/mitigations/d3fend"
D3FEND_MITIGATION_COLLECTION = "d3fend_mitigation"
D3FEND_MITIGATIONS_FILE_PATH = os.path.join(
    OUT_DIR, f"{D3FEND_MITIGATION_COLLECTION}.json"
)
D3FEND_MITIGATIONS_TECHNIQUE_COLLECTION_KEYS = ("d3fend_mitigation", "technique")
D3FEND_MITIGATIONS_TECHNIQUE_COLLECTION = get_edge_collection_name(
    *D3FEND_MITIGATIONS_TECHNIQUE_COLLECTION_KEYS
)
D3FEND_MITIGATIONS_TECHNIQUE_FILE_PATH = os.path.join(
    OUT_DIR, f"{D3FEND_MITIGATIONS_TECHNIQUE_COLLECTION}.json"
)


def download_data():
    _download_technique_tree()
    _download_ontologies()


def _download_technique_tree():
    logging.info(f"Begin download of {D3FEND_TECHNIQUE_TREE}")
    response = requests.get(D3FEND_TECHNIQUE_TREE)
    file_path = os.path.join(OUT_DIR, os.path.basename(D3FEND_TECHNIQUE_TREE))
    with open(file_path, "wb") as fd:
        for chunk in response.iter_content(chunk_size=128):
            fd.write(chunk)

    assert os.path.exists(file_path)
    logging.info(f"Downloaded {D3FEND_TECHNIQUE_TREE} to {file_path}")


def _download_ontologies():
    logging.info(f"Begin download of {D3FEND_ONTOLOGY_JSON}")
    # D3FENDThing is top of knowledge hierarchy
    response = requests.get(D3FEND_ONTOLOGY_JSON)
    file_path = os.path.join(OUT_DIR, os.path.basename(D3FEND_ONTOLOGY_JSON))
    with open(file_path, "w") as fd:
        json.dump(response.json(), fd, indent=4)

    assert os.path.exists(file_path)

    for ontology in (D3FEND_ONTOLOGY_OWL, D3FEND_ONTOLOGY_TTL):
        response = requests.get(ontology)
        file_path = os.path.join(OUT_DIR, os.path.basename(ontology))
        with open(file_path, "wb") as fd:
            for chunk in response.iter_content(chunk_size=128):
                fd.write(chunk)

        assert os.path.exists(file_path)
        logging.info(f"Store {ontology} to {file_path}")


def update_BRON_graph_db(
    username: str, password: str, ip: str, validation: bool = False
) -> None:
    logging.info(f"Begin update of {ip} with D3FEND")
    g = rdflib.Graph()
    _ = g.parse(D3FEND_ONTOLOGY_TTL, format="turtle")
    # d3_id -> name
    mitigations = find_mitigations(g)
    # name -> d3_id
    mitigation_name_id_map = dict([(v, k) for k, v in mitigations.items()])
    mitigation_id_graph_db_id_map = {}
    bron_mitigations = []
    # TODO is this really a good key. Should use the d3fend-id as key instead of internal BRON counter
    mitigation_ids = sorted(list(mitigations.keys()))
    if validation:
        schema = get_schema("d3fend_mitigation")

    for cnt, mitigation_id in enumerate(mitigation_ids):
        mitigation = mitigations[mitigation_id]
        label = find_mitigation_label(mitigation, g)
        graph_db_id = f"{D3FEND_MITIGATION_COLLECTION}_{cnt:05}"
        entry = {
            "_key": graph_db_id,
            "name": str(label),
            "metadata": {"d3fend-id": str(mitigation_id)},
            "original_id": str(mitigation),
            "datatype": D3FEND_MITIGATION_COLLECTION,
        }
        if validation:
            validate_entry(entry, schema)

        bron_mitigations.append(entry)
        mitigation_id_graph_db_id_map[str(mitigation)] = graph_db_id

    with open(D3FEND_MITIGATIONS_FILE_PATH, "w") as fd:
        for bron_mitigation in bron_mitigations[:]:
            json.dump(bron_mitigation, fd)
            fd.write("\n")

    assert os.path.exists(D3FEND_MITIGATIONS_FILE_PATH)
    logging.info(f"Created {D3FEND_MITIGATION_COLLECTION}")

    mitigation_capecs = []
    mitigation_technique_map, _ = find_techniques_from_mitigations()
    client = arango.ArangoClient(hosts=f"http://{ip}:8529")
    db = client.db(DB, username=username, password=password, auth_method="basic")
    create_graph(
        username,
        password,
        ip,
        (D3FEND_MITIGATION_COLLECTION,),
        ((D3FEND_MITIGATION_COLLECTION, "technique"),),
    )
    if validation:
        schema = get_schema("D3fend_mitigationTechnique")

    for mitigation, techniques in mitigation_technique_map.items():
        mitigation_id = mitigation_name_id_map[mitigation]
        mitigation_graph_db_id = mitigation_id_graph_db_id_map[mitigation]
        for technique in techniques:
            technique_id = get_technique_id_from_id(technique, db)
            if technique_id is None:
                logging.error(f"No entry corresponding to {technique}")
                continue

            entry = {
                "_from": f"{technique_id}",
                "_to": f"{D3FEND_MITIGATION_COLLECTION}/{mitigation_graph_db_id}",
            }
            if validation:
                validate_entry(entry, schema)

            mitigation_capecs.append(entry)

    with open(D3FEND_MITIGATIONS_TECHNIQUE_FILE_PATH, "w") as fd:
        for mitigation_capec in mitigation_capecs[:]:
            json.dump(mitigation_capec, fd)
            fd.write("\n")

    assert os.path.exists(D3FEND_MITIGATIONS_TECHNIQUE_FILE_PATH)
    logging.info(f"Created {D3FEND_MITIGATION_COLLECTION} edges")
    import_into_arango(
        username,
        password,
        ip,
        D3FEND_MITIGATIONS_FILE_PATH,
        False,
        D3FEND_MITIGATION_COLLECTION,
    )
    update_graph_in_graph_db(
        username, password, ip, collection_name=D3FEND_MITIGATION_COLLECTION
    )
    import_into_arango(
        username,
        password,
        ip,
        D3FEND_MITIGATIONS_TECHNIQUE_FILE_PATH,
        True,
        D3FEND_MITIGATIONS_TECHNIQUE_COLLECTION,
    )
    update_graph_in_graph_db(
        username, password, ip, edge_key=D3FEND_MITIGATIONS_TECHNIQUE_COLLECTION_KEYS
    )
    logging.info(f"Done with adding D3FEND mitigations to {ip}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # Download defend data https://d3fend.mitre.org/resources/
    download_data()


def parse_args(args: List[str]) -> Any:
    parser = argparse.ArgumentParser(description="Link D3FEND mitigations to BRON")
    parser.add_argument("--username", type=str, required=True, help="DB username")
    parser.add_argument("--password", type=str, required=True, help="DB password")
    parser.add_argument("--ip", type=str, required=True, help="DB IP address")
    parser.add_argument(
        "--arango_import",
        action="store_true",
        help="Create mitigation files and use arangoimport with created json files. Requires `arangoimport`.",
    )
    args = parser.parse_args(args)
    return args


if __name__ == "__main__":
    log_file = os.path.basename(__file__).replace(".py", ".log")
    logging.basicConfig(
        format="%(asctime)s %(levelname)s: %(message)s",
        level=logging.INFO,
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )

    args_ = parse_args(sys.argv[1:])
    if args_.arango_import:
        update_BRON_graph_db(args_.username, args_.password, args_.ip)
    else:
        main()
