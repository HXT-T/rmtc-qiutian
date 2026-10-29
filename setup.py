from setuptools import setup

setup(name="rmtc_bot", version="1.0.0", packages=["rmtc_bot"],
      data_files=[("share/ament_index/resource_index/packages", ["resource/rmtc_bot"]),
                  ("share/rmtc_bot", ["package.xml"])],
      install_requires=["setuptools"], python_requires=">=3.10",
      entry_points={"console_scripts": ["rmtc_bot = rmtc_bot.__main__:main"]},
      description="RMTC public-protocol autonomous contestant", license="MIT")
