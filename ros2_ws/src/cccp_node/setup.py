from setuptools import find_packages, setup


package_name = "cccp_node"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools", "pysyncobj", "pyyaml"],
    zip_safe=False,
    maintainer="CCCP Team",
    maintainer_email="comp@example.com",
    description="CCCP cluster node: replicated task table, leader control plane, gateway and resource monitor.",
    license="Proprietary",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "control_node = cccp_node.ros.control_node:main",
        ],
    },
)
