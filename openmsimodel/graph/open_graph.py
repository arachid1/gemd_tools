import json
import networkx as nx
from collections import defaultdict
import shutil
import os
import matplotlib.pyplot as plt
import argparse
import pathlib

from gemd.util.impl import recursive_foreach
from gemd.json import GEMDJson

from openmsimodel.utilities.argument_parsing import OpenMSIModelParser
from openmsimodel.utilities.runnable import Runnable
from openmsimodel.graph.helpers import launch_graph_widget
from openmsimodel.utilities.io import read_gemd_data

import time


class OpenGraph(Runnable):
    """
    Provides modules to build and visualize a networkx or graphviz object from GEMD objects.

    By taking a folder path containing GEMD thin JSON files, this class establishes the relationships
    between them by interpreting their uuids/links. It produces outputs ranging from
    an SVG image or GraphML file with simple labels to dot products containing all GEMD assets with flexibility,
    including attributes, file links, or tags.
    """

    ARGUMENT_PARSER_TYPE = OpenMSIModelParser

    IPYNB_FILENAME = pathlib.Path(
        pathlib.Path(__file__).parent.resolve()
        / "open_graph_nb/open_graph_nb_template.ipynb"
    )
    CONFIG_FILENAME = pathlib.Path(
        pathlib.Path(__file__).parent.resolve() / "open_graph_nb/.config"
    )

    # TODO: move build_graph function params to obj + store pygraphviz and networkx as obj attr
    def __init__(
        self,
        name,
        source,
        output,
        science_kit=None,
        layout="raw",
        add_bidirectional_edges=False,
        take_small_sample=False,
        add_separate_node=False,
        which="run",
        assets_to_add={
            "add_attributes": 1,
            "add_file_links": 1,
            "add_tags": 1,
        },
        dump_svg_and_dot=False,
    ):
        """
        Initialize the OpenGraph object with provided parameters.

        :param name: Name of the graph.
        :type name: str
        :param source: source of GEMD data,
        :type source: str or list
        :param output: Path for saving output files.
        :type output: str
        :param layout: The layout parameter for the graph visualization.
        :type layout: str
        :param add_bidirectional_edges: Flag to add bidirectional edges between nodes.
        :type add_bidirectional_edges: bool
        :param take_small_sample: Flag to take a small sample of data for the graph.
        :type take_small_sample: bool
        :raises FileNotFoundError: If the output path does not exist.
        """
        self.name = name
        self.source = pathlib.Path(source) if not (type(source) == list) else source
        self.output = pathlib.Path(output)  # TODO: REQUIRES FULL PATH NOW; fix or keep?
        if not self.output.exists():
            raise FileNotFoundError(f"{self.output} does not exist.")
        self.science_kit = science_kit
        if self.science_kit:
            self.science_kit.open_graphs[name] = self
        self.layout = layout
        self.add_bidirectional_edges = add_bidirectional_edges
        self.take_small_sample = take_small_sample
        self.add_separate_node = add_separate_node
        self.which = which
        self.assets_to_add = assets_to_add
        self.dump_svg_and_dot = dump_svg_and_dot
        self.svg_path = None
        self.dot_path = None
        self.graphml_path = None
        self.shapes = {"run": "circle", "spec": "rectangle", "template": "triangle"}

    # instance method
    def build_graph(self, save=False, uuid_to_track="auto"):
        """
        Creates a NetworkX graph representation of the GEMD relationships.

        It reads every object generated by the GEMDEncoder object, storing all of its links by uid,
        and forming directed relationships, such as ingredient->process or process->material.
        It allows for filtering the mapped objects and saves a NetworkX graph in "dot" format.

        :param uuid_to_track: The uuid to track in the objects.
        :type uuid_to_track: str, optional
        :returns: A tuple of the NetworkX graph, a PyGraphVIZ graph, and a name mapping dictionary.
        :rtype: tuple
        """

        print(
            "-- Building {}s of {}".format(
                self.which,
                self.source
                if not type(self.source) == list
                else f"list with {len(self.source)} items",
            )
        )
        G_nx = nx.DiGraph(name=self.name)
        object_mapping = defaultdict()
        name_mapping = defaultdict()
        encoder = GEMDJson()
        nb_disregarded = 0

        gemd_objects, gemd_paths = read_gemd_data(self.source, encoder)

        if len(gemd_objects) == 0:
            print("No objects were found.")
            return

        full_length = len(gemd_objects)
        quarter_length = int(full_length / 4)
        if self.take_small_sample:
            gemd_objects, gemd_paths = (
                gemd_objects[:quarter_length],
                gemd_paths[:quarter_length],
            )

        # adding objects to graph one by one
        for i, obj_data in enumerate(gemd_objects):
            obj_type = obj_data["type"]
            obj_state = obj_type.split("_")[-1]
            if not (uuid_to_track in obj_data["uids"].keys()):
                continue
            obj_uid = obj_data["uids"][uuid_to_track]
            obj_name = obj_data["name"]
            name_mapping[obj_uid] = "{} [{}, {}]".format(obj_name, obj_uid, obj_type)
            self.handle_gemd_obj(
                G_nx,
                obj_uid,
                obj_data,
                obj_type,
                obj_state,
            )
            try:
                path = gemd_paths[i]
                self.add_to_graph(G_nx, obj_uid, "source", path.name)
            except IndexError:
                continue
            if i % quarter_length == 0:
                print("{}/{} gemd objects processed...".format(i, full_length))
        print("Done.")

        # relabelling according to uid -> name
        relabeled_G_nx = G_nx
        if name_mapping:
            print("Relabeling nodes ...")
            relabeled_G_nx = nx.relabel_nodes(G_nx, name_mapping)

        # converting to grapviz
        relabeled_G_gviz = None
        if self.dump_svg_and_dot:
            print("Generating Graphviz version...")
            relabeled_G_gviz = self.map_to_graphviz(relabeled_G_nx)

        # # plotting
        if save:
            dot_path, svg_path, graphml_path = self.save_graph(
                self.output,
                relabeled_G_nx,
                relabeled_G_gviz,
                "{}_{}".format(self.name, self.which),
                dump_svg_and_dot=self.dump_svg_and_dot,
            )

            self.update_paths(svg_path, dot_path, graphml_path)

        # info
        self.diagnostics(relabeled_G_nx, gemd_objects, nb_disregarded)

        return relabeled_G_nx, relabeled_G_gviz, name_mapping

    def handle_gemd_obj(
        self,
        G,
        uid,
        obj_data,
        obj_type,
        obj_state,
    ):
        """method to handle the addition of a gemd object

        Args:
            G (NetworkX graph): graph
            uid (str): uid of current object
            obj_data (dict): data of current object
            obj_type (str): type of current object
        """
        if obj_type.startswith("process"):
            if obj_type.endswith(self.which) or self.which == "all":
                G.add_node(uid, color="red", shape=self.shapes[obj_state])
                self.add_gemd_assets(
                    G,
                    uid,
                    obj_data,
                    obj_type,
                )
        elif obj_type.startswith("ingredient"):  # TODO if node doesn't exist, create?
            if obj_type.endswith(self.which) or self.which == "all":
                G.add_node(uid, color="blue", shape=self.shapes[obj_state])
                process = obj_data["process"]["id"]
                G.add_edge(uid, process)
                if "material" in obj_data and obj_data["material"]:
                    material = obj_data["material"]["id"]
                    G.add_edge(material, uid)
                if self.add_bidirectional_edges:
                    G.add_edge(process, uid)
                    if "material" in obj_data and obj_data["material"]:
                        material = obj_data["material"]["id"]
                        G.add_edge(material, uid)
                        if self.add_bidirectional_edges:
                            G.add_edge(uid, material)
                self.add_gemd_assets(
                    G,
                    uid,
                    obj_data,
                    obj_type,
                )
        elif obj_type.startswith("material"):
            if obj_type.endswith(self.which) or self.which == "all":
                G.add_node(uid, color="green", shape=self.shapes[obj_state])
                self.add_gemd_assets(
                    G,
                    uid,
                    obj_data,
                    obj_type,
                )
                # if not self.restrictive:
                if "process" in obj_data and obj_data["process"]:
                    process = obj_data["process"]["id"]
                    G.add_edge(process, uid)  # ?
                    if self.add_bidirectional_edges:
                        G.add_edge(uid, process)
        elif obj_type.startswith("measurement"):
            if obj_type.endswith(self.which) or self.which == "all":
                G.add_node(uid, color="purple", shape=self.shapes[obj_state])
                self.add_gemd_assets(
                    G,
                    uid,
                    obj_data,
                    obj_type,
                )
                # if not self.restrictive:
                if "material" in obj_data and obj_data["material"]:
                    material = obj_data["material"]["id"]
                    G.add_edge(material, uid)
                    if self.add_bidirectional_edges:
                        G.add_edge(uid, material)

        if self.which == "all":
            if (
                obj_type.startswith("condition")
                or obj_type.startswith("parameter")
                or obj_type.startswith("property")
            ):
                G.add_node(uid, color="black", shape="trapezium")
                self.add_gemd_assets(
                    G,
                    uid,
                    obj_data,
                    obj_type,
                )
            # adding attribute templates from object templates
            elif obj_type.endswith("template"):
                if "parameters" in obj_data and obj_data["parameters"]:
                    for parameter in obj_data["parameters"]:
                        G.add_edge(uid, parameter[0]["id"])
                        if self.add_bidirectional_edges:
                            G.add_edge(parameter[0]["id"], uid)
                if "conditions" in obj_data and obj_data["conditions"]:
                    for condition in obj_data["conditions"]:
                        G.add_edge(uid, condition[0]["id"])
                        if self.add_bidirectional_edges:
                            G.add_edge(condition[0]["id"], uid)
                if "properties" in obj_data and obj_data["properties"]:
                    for prop in obj_data["properties"]:
                        G.add_edge(uid, prop[0]["id"])
                        if self.add_bidirectional_edges:
                            G.add_edge(prop[0]["id"], uid)

            if obj_type.endswith("run"):
                if "spec" in obj_data and "id" in obj_data["spec"]:
                    spec_id = obj_data["spec"]["id"]
                    G.add_edge(uid, spec_id)
                    if self.add_bidirectional_edges:
                        G.add_edge(spec_id, uid)
            elif obj_type.endswith("spec"):
                if (
                    "template" in obj_data
                    and obj_data["template"] is not None
                    and "id" in obj_data["template"]
                ):
                    template_id = obj_data["template"]["id"]
                    G.add_edge(uid, template_id)
                    if self.add_bidirectional_edges:
                        G.add_edge(template_id, uid)

    def add_gemd_assets(
        self,
        G,
        uid,
        obj_data,
        obj_type,
    ):
        self.add_to_graph(G, uid, "uuid", uid)
        self.add_to_graph(G, uid, "type", obj_type)
        if self.layout == "raw":
            self.add_to_graph(G, uid, "object", json.dumps(obj_data))
        elif self.layout == "visualization":
            if self.assets_to_add["add_attributes"] and not (
                obj_type.endswith("template")
            ):
                if "parameters" in obj_data:
                    self.handle_gemd_value(G, uid, obj_data["parameters"])
                if "properties" in obj_data:
                    self.handle_gemd_value(G, uid, obj_data["properties"])
                if "conditions" in obj_data:
                    self.handle_gemd_value(G, uid, obj_data["conditions"])
            if self.assets_to_add["add_file_links"] and "file_links" in obj_data:
                self.handle_gemd_value(G, uid, obj_data["file_links"])
            if self.assets_to_add["add_tags"] and "tags" in obj_data:
                self.handle_gemd_value(G, uid, obj_data["tags"])

    def handle_gemd_value(self, G, uid, assets):
        # TODO: add pointing to templates?
        for att in assets:
            if type(att) in [list]:
                continue
            if type(att) in [str]:
                if "::" in att:  # is a gemd tag
                    self.add_to_graph(G, uid, "tags", att)
            elif att["type"]:  # is a gemd object
                # reading gemd file links
                if att["type"] == "file_link":
                    self.add_to_graph(G, uid, "file_links", att["url"])
                    continue
                # reading gemd attributes
                if att["type"] == "property_and_conditions":
                    value = att["property"]["value"]
                    att_name = att["property"]["name"]
                else:
                    value = att["value"]
                    att_name = att["name"]
                if value["type"] == "nominal_real":
                    node_name = "{}, {} {}".format(
                        att_name, value["nominal"], value["units"]
                    )
                elif value["type"] == "nominal_integer":
                    node_name = "{}, {}".format(att_name, value["nominal"])
                elif value["type"] == "uniform_real":
                    node_name = "{}, {}-{} {}".format(
                        att_name,
                        value["lower_bound"],
                        value["upper_bound"],
                        value["units"],
                    )
                elif (
                    value["type"] == "uniform_integer"
                ):  # FIXME same as above without units
                    node_name = "{}, {}-{}".format(
                        att_name,
                        value["lower_bound"],
                        value["upper_bound"],
                    )
                elif (
                    value["type"] == "empirical_formula"
                ):  # FIXME same as above without units
                    node_name = "{}, {}, {}".format(
                        att_name,
                        value["formula"],
                        value["type"],
                    )
                elif (
                    value["type"] == "normal_real"
                ):  # FIXME same as above without units
                    node_name = "{}, mean {}, std {}, {}, {}".format(
                        att_name,
                        value["mean"],
                        value["std"],
                        value["units"],
                        value["type"],
                    )
                elif value["type"] == "nominal_categorical":
                    node_name = "{}, {}".format(att_name, value["category"])
                elif value["type"] == "nominal_composition":
                    node_name = "{}, {}".format(att_name, value["quantities"])

                self.add_to_graph(G, uid, att_name, node_name)

    def add_to_graph(self, G, uid, att_name, node_name):
        if self.add_separate_node == True:  # add as a separate node
            G.add_node(node_name, s="rectangle", color="orange")
            G.add_edge(uid, node_name)
            if self.add_bidirectional_edges:
                G.add_edge(node_name, uid)
        else:  # add as an attribute of the node
            if uid in G.nodes.keys():
                if att_name in G.nodes[uid].keys():  # already exists, append to it
                    if type(G.nodes[uid][att_name]) == dict:
                        count = len(G.nodes[uid][att_name])
                        G.nodes[uid][att_name][count] = node_name
                        # G.add_node_attribute(uid, att_name, node_name)
                    return
                if att_name in ["file_links", "tags"]:
                    G.nodes[uid][att_name] = {0: node_name}
                else:
                    G.nodes[uid][att_name] = node_name

    def diagnostics(self, G, gemd_objects, nb_disregarded):
        print("-- Analysis --")
        if not self.add_bidirectional_edges:
            print("cycles in the graph: {}".format(list(nx.simple_cycles(G))))
        print(
            "disregarded/total number of gemd objects: {}/{}".format(
                nb_disregarded, len(gemd_objects)
            )
        )
        subgraphs = [G.subgraph(c) for c in nx.strongly_connected_components(G)]
        print("number of connected components: {}".format(len(subgraphs)))
        print("total nb of isolates in the graph: {}".format(nx.number_of_isolates(G)))

    @classmethod
    def launch(cls, path, from_command_line=False):
        print("Launching {}...".format(path))
        if from_command_line:
            # reading path to dot/graphml
            config_file_path = (
                cls.CONFIG_FILENAME
            )  # TODO: add option to just pass another param?
            try:
                with open(config_file_path, "w") as f:
                    f.write(path)
            except FileNotFoundError:
                print(f"Configuration file '{config_file_path}' not found.")
            except Exception as e:
                print(
                    f"An error occurred while reading the configuration file: {str(e)}"
                )

            folder_path = cls.IPYNB_FILENAME.parent
            if not os.path.exists(folder_path):
                print(f"The folder '{folder_path}' does not exist.")
                return
            if not os.listdir(folder_path):
                print(f"The folder '{folder_path}' is empty.")
                return
            print(f"Contents of the folder '{folder_path}':")
            for item in os.listdir(folder_path):
                print(item)
            os.system("jupyter notebook --notebook-dir={}".format(folder_path))
            return None
        else:
            launch_graph_widget(path)

    def update_paths(self, svg_path, dot_path, graphml_path):
        self.svg_path = svg_path
        self.dot_path = dot_path
        self.graphml_path = graphml_path

    @classmethod
    def map_to_graphviz(cls, G):
        """helper method to map NetworkX graph to Graphviz graph

        Args:
            G (_type_): _description_
            name_mapping (_type_, optional): _description_. Defaults to None.

        Returns:
            _type_: _description_
        """

        G = nx.nx_agraph.to_agraph(G)
        G.layout(prog="dot")
        return G

    @classmethod
    def slice_subgraph(cls, G, uuid, funcs, add_current=True):
        """applies paseed function(s) to graph object of interest with uuid=uuid.
        If elements are found to match the criteria, a subgraph containing all those elements is returned

        Args:
            G (NetworkX graph): knowledge graph in questoin
            uuid (str): uuid of current element of interest on self.which the functions are applied
            funcs (list): list of function(s) to apply to graph
            add_current (bool, optional): whether or not to add the current element of interest. Defaults to True.

        G (NetworkX graph): Graph to save
        """
        els = set()
        for func in funcs:
            els = els.union(func(G, uuid))
        if add_current:
            els.add(uuid)
        return G.subgraph(els)

    @classmethod
    def return_uuid(cls, identifier):
        """return the identifier of interest.

        Args:
            identifier (str): identifier of object of interest

        Returns:
            str: identifier
        """
        return identifier

    @classmethod
    def extract_subgraph(cls, G, identifier, func):
        """extract subgraph from graph knowledge, based on functions applied to element of interest to filter in additional desired elements.
        Examples includes neighbords, descendants, ancestors, etc.

        Args:
            G (NetworkX graph): Graph to save
            identifier (str): uuid or identifier of element of interest
            func (func): function to determine whether graph element should be added to subgraph or not

        Returns:
            NetworkX graph: subgraph filtered based on passed criteria
        """
        uuid = cls.return_uuid(identifier)
        return cls.slice_subgraph(G, uuid, func)

    @classmethod
    def save_graph(cls, dest, G_nx, G_gviz, name, dump_svg_and_dot=False):
        """class method to save Graphviz graph.

        Args:
            dest (Pathlib.Path): path where to save the graph
            G_nx (Networkx graph): Networkx version of graph
            G_gviz (Graphviz graph): Graphviz version of graph
            name (str): name of file to save graph to

        Returns:
            str: paths to, respectively, the dot and svg files
        """
        print("Saving graphs...")

        svg_path = os.path.join(dest, "{}.svg".format(name))
        dot_path = os.path.join(dest, "{}.dot".format(name))
        graphml_path = os.path.join(dest, "{}.graphml".format(name))

        if G_gviz is not None and dump_svg_and_dot:
            # writing svg file
            print("Dumping svg...")
            start = time.time()
            G_gviz.draw(svg_path)
            plt.close()
            end = time.time()
            print(f"Time elapsed: {end - start}")

            # writing dot file
            print("Dumping dot...")
            start = time.time()
            with open(dot_path, "w") as f:
                f.write(str(G_gviz))
            end = time.time()
            print(f"Time elapsed: {end - start}")
            print("-- Saved graph to {} and {} ".format(dot_path, svg_path))
        else:
            print("Couldn't find GraphViz graph. Can't dump SVG and DOT files.")

        if G_nx is not None:
            # writing graphml
            print("Dumping graphml...")
            start = time.time()

            def dicts_to_str(G):
                if G.nodes:
                    for node_name in G.nodes:
                        if "tags" in G.nodes[node_name] and G.nodes[node_name]["tags"]:
                            G.nodes[node_name]["tags"] = str(G.nodes[node_name]["tags"])
                        if (
                            "file_links" in G.nodes[node_name]
                            and G.nodes[node_name]["file_links"]
                        ):
                            G.nodes[node_name]["file_links"] = str(
                                G.nodes[node_name]["file_links"]
                            )
                return G

            # nx.write_graphml_lxml(dicts_to_str(G_nx), graphml_path)
            nx.write_graphml_lxml(G_nx, graphml_path, named_key_ids=True)
            end = time.time()
            print(f"Time elapsed: {end - start}")
            print("-- Saved graph to {}".format(graphml_path))
        else:
            print("Couldn't find NetworkX graph.")

        return dot_path, svg_path, graphml_path

    @classmethod
    def get_argument_parser(cls, *args, **kwargs):
        parser = cls.ARGUMENT_PARSER_TYPE(*args, **kwargs)
        cl_args, cl_kwargs = cls.get_command_line_arguments()
        parser.add_arguments(*cl_args, **cl_kwargs)
        return parser

    @classmethod
    def get_command_line_arguments(cls):
        superargs, superkwargs = super().get_command_line_arguments()
        args = [
            *superargs,
            "name",
            "source",
            "which",
            "identifier",
            "launch_notebook",
            "add_attributes",
            "add_file_links",
            "add_tags",
            "add_separate_node",
            "add_bidirectional_edges",
            "layout",
            "a",
            "d",
            "uuid_to_track",
            "output",
            "take_small_sample",
            "dump_svg_and_dot",
        ]
        kwargs = {**superkwargs}
        return args, kwargs

    @classmethod
    def run_from_command_line(cls, args=None):
        """
        Run a :class:`~OpenGraph` directly from the command line
        Calls :func:`~reconstruct` on a :class:`~OpenGraph` defined by
        command line (or given) arguments
        :param args: the list of arguments to send to the parser instead of getting them from sys.argv
        :type args: list, optional
        """
        parser = cls.get_argument_parser()
        args = parser.parse_args(args=args)
        viewer = cls(
            args.name,
            args.source,
            args.output,
            args.layout,
            args.add_bidirectional_edges,
            args.take_small_sample,
            dump_svg_and_dot=args.dump_svg_and_dot,
        )
        viewer.assets_to_add = {
            "add_attributes": args.add_attributes,
            "add_file_links": args.add_file_links,
            "add_tags": args.add_tags,
        }
        G, relabeled_G_gviz, name_mapping = viewer.build_graph(
            uuid_to_track=args.uuid_to_track,
        )

        # reduces to elements with identifier and related nodes
        if args.identifier:
            functions = []
            if args.d:
                functions.append(nx.descendants)
            if args.a:
                functions.append(nx.ancestors)
            identifier_G = cls.extract_subgraph(G, args.identifier, func=functions)
            identifier_G_dot_path, _, identifier_G_grapml_path = cls.save_graph(
                args.output,
                identifier_G,
                None,  # FIXME
                "{}".format(args.identifier),
                dump_svg_and_dot=viewer.dump_svg_and_dot,
            )

        # launches interactive notebook
        if args.launch_notebook:
            if args.identifier:
                viewer.launch(identifier_G_grapml_path, from_command_line=True)
            else:
                viewer.launch(viewer.graphml_path, from_command_line=True)


def main(args=None):
    """
    Main method to run from command line
    """
    OpenGraph.run_from_command_line(args)


if __name__ == "__main__":
    main()
