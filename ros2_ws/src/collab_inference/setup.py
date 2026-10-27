from glob import glob
from setuptools import find_packages, setup


package_name = "collab_inference"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml", "README.md"]),
        ("share/" + package_name + "/weights", glob("weights/*")),
    ],
    package_data={"collab_infer.pkci": ["classes.json"]},
    install_requires=["setuptools", "numpy", "pillow"],
    zip_safe=False,
    maintainer="CCCP Team",
    maintainer_email="comp@example.com",
    description="ROS 2 wrappers for PKCI and PKCT collaborative inference.",
    license="Proprietary",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "pkci_server = collab_inference_ros.pkci_server:main",
            "pkct_server = collab_inference_ros.pkct_server:main",
        ]
    },
)
