from setuptools import find_packages, setup


package_name = "cccp_task_executor"

setup(
    name=package_name,
    version="0.2.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools", "numpy", "pillow"],
    zip_safe=False,
    maintainer="CCCP Team",
    maintainer_email="comp@example.com",
    description="Board-side executor for dispatched CCCP PKCI/PKCT tasks.",
    license="Proprietary",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "task_executor = cccp_task_executor.executor_node:main",
            "data_plane = cccp_task_executor.data_plane:main",
            "airsim_camera = cccp_task_executor.airsim_camera:main",
        ]
    },
)
