"""
Created by Sara Geleskie Damiano
"""
#%%
import sys
import logging

# import time
import json
import copy
from typing import Dict
import pytz
from datetime import datetime

import pandas as pd

import geopandas as gpd
import shapely
from shapely.geometry.multipolygon import MultiPolygon

from modelmw_client import *
from soupsieve import closest

#%%
# Set up the API client
from mmw_secrets import (
    srgd_staging_api_key,
    srgd_mmw_user,
    srgd_mmw_pass,
    save_path,
    csv_path,
    json_dump_path,
    csv_extension,
)

# Create an API user
mmw_run = ModelMyWatershedAPI(srgd_staging_api_key, save_path, True)
# Authenticate with MMW
mmw_run.login(mmw_user=srgd_mmw_user, mmw_pass=srgd_mmw_pass)

land_use_layer = "2019_2019"
# ^^ NOTE:  This is the default, but I also specified it in the code below
stream_layer = "nhdhr"
# ^^ NOTE:  This is the default.  I did not specify a stream override.
weather_layer = "NASA_NLDAS_2000_2019"

#%%
# Read location data - shapes from national map
# These are all of the HUC-12's in the HUC-6's 020401, 020402, and 020403
# I got the list from https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/6/query?f=json&where=((UPPER(huc12)%20LIKE%20%27020401%25%27)%20OR%20(UPPER(huc12)%20LIKE%20%27020402%25%27)%20OR%20(UPPER(huc12)%20LIKE%20%27020403%25%27))&spatialRel=esriSpatialRelIntersects&outFields=OBJECTID%2Csourcefeatureid%2Cloaddate%2Careaacres%2Careasqkm%2Cstates%2Chuc12%2Cname%2Chutype%2Chumod%2Ctohuc%2Cnoncontributingareaacres%2Cnoncontributingareasqkm&orderByFields=OBJECTID%20ASC&outSR=102100

huc12_shapes = gpd.read_file(
    "HUC12s in 020401, 020402, 020403 v2.json",
)
huc12_shapes = huc12_shapes.set_crs("EPSG:3857").to_crs("EPSG:4326")
huc12_shapes["huc_level"] = 12
huc12_shapes = (
    huc12_shapes.sort_values(by=["huc12"])
    .reset_index(drop=True)
    .rename(columns={"huc12": "huc"})
)

#%%
# These are all of the HUC-10's in the HUC-6's 020401, 020402, and 020403
# I got the list from https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/5/query?f=json&where=((UPPER(huc10) LIKE '020401%') OR (UPPER(huc10) LIKE '020402%') OR (UPPER(huc10) LIKE '020403%'))&spatialRel=esriSpatialRelIntersects&outFields=OBJECTID,Shape,sourcedatadesc,sourceoriginator,sourcefeatureid,loaddate,areaacres,areasqkm,states,huc10,name,hutype,humod,referencegnis_ids&orderByFields=OBJECTID ASC&outSR=102100

huc10_shapes = gpd.read_file(
    "WBD_HUC10s.json",
)
huc10_shapes = huc10_shapes.set_crs("EPSG:3857").to_crs("EPSG:4326")
huc10_shapes["huc_level"] = 10
huc10_shapes = (
    huc10_shapes.sort_values(by=["huc10"])
    .reset_index(drop=True)
    .rename(columns={"huc10": "huc"})
)

#%%
# Read location data - list from Michael Campagna
hucs_from_Mike = pd.read_csv(save_path + "huc12_list_drwipolassess.csv").rename(
    columns={"huc12": "huc"}
)
huc12_shapes["huc_level"] = 12

#%%
# Fix huc name strings
for frame in [huc12_shapes, huc10_shapes, hucs_from_Mike]:
    frame["huc"] = frame["huc"].astype(str)
    frame.loc[~frame["huc"].str.startswith("0"), "huc"] = (
        "0" + frame.loc[~frame["huc"].str.startswith("0")]["huc"]
    )
hucs_to_run = pd.concat([huc12_shapes, huc10_shapes], ignore_index=True)

#%% Set up logging
log_file = save_path + "run_gwlfe_srat_drb_v2.log"

logging.basicConfig(filename=log_file, encoding="utf-8", level=logging.INFO)

root = logging.getLogger()
root.setLevel(logging.INFO)
logging.getLogger("modelmw_client").setLevel(logging.WARN)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
root.addHandler(handler)

this_run_start = (
    pytz.utc.localize(datetime.utcnow())
    .astimezone(pytz.timezone("US/Eastern"))
    .strftime("%Y-%m-%d %H:%M:%S %z")
)


logging.info("Starting script at {}".format(this_run_start))

#%%
# helper functions
def read_or_run_mapshed(endpoint, label, payload):
    job_dict = None
    job_dict, result = mmw_run.read_dumped_result(
        endpoint,
        label,
    )
    if job_dict is None:
        logging.info("  Running Mapshed ({})".format(endpoint))
        job_dict: ModelMyWatershedJob = mmw_run.run_mmw_job(
            request_endpoint=endpoint,
            job_label=label,
            payload=payload,
        )
    if job_dict is not None and "result_response" in job_dict.keys():
        if "WeatherStations" in job_dict["result_response"]["result"].keys():
            station_list = [
                sta["station"]
                for sta in job_dict["result_response"]["result"]["WeatherStations"]
            ]
            station_list.sort()
            weather_stations = ",".join(map(str, station_list))
        else:
            weather_stations = None
        return (
            job_dict["result_response"]["job_uuid"],
            weather_stations,
        )

    return None, None


def run_gwlfe(endpoint, label, mapshed_job_id, modifications):
    logging.info("  Running GWLF-E ({})".format(endpoint))
    gwlfe_payload: Dict = {
        # NOTE:  The value of the inputmod_hash doesn't really matter here
        # Internally, the ModelMW site uses the inputmod_hash in scenerios to
        # determine whether it can use cached results or if it needs to
        # re-run the job so the value is meaningless here
        "inputmod_hash": mmw_run.inputmod_hash,
        "modifications": modifications,
        "job_uuid": mapshed_job_id,
    }
    gwlfe_job_dict: ModelMyWatershedJob = mmw_run.run_mmw_job(
        request_endpoint=endpoint,
        job_label=label,
        payload=gwlfe_payload,
    )
    if "result_response" in gwlfe_job_dict.keys():
        gwlfe_result_raw = gwlfe_job_dict["result_response"]
        gwlfe_result = copy.deepcopy(gwlfe_result_raw)["result"]
        logging.info("  --Got GWLF-E ({}) results".format(endpoint))
        return gwlfe_job_dict, gwlfe_result
    return gwlfe_job_dict, None


def get_weather_modifications(huc_row, mapshed_job_id, layer_overrides):
    gwlfe_mods = [{}]
    used_weather_layer = "USEPA_1960_1990"

    # With a mapshed job and a HUC shape, we create a project so we can get the weather data for it
    logging.info("  Creating a new project")
    project_dict: Dict = mmw_run.create_project(
        model_package="gwlfe",
        area_of_interest=shapely.geometry.mapping(MultiPolygon([huc_row["geometry"]])),
        name=huc_row["huc"],
        mapshed_job_uuid=mapshed_job_id,
        layer_overrides=layer_overrides,
    )
    project_id: str = project_dict["id"] if "id" in project_dict.keys() else None
    if project_id is None:
        logging.info(
            "*** Couldn't create a project for {} ({})".format(
                huc_row["huc"], huc_row["name"]
            )
        )
        return used_weather_layer, gwlfe_mods
    logging.info("  --Project {} created".format(project_id))

    # with a project ID in hand, we can get the 2000-2019 weather data,
    # which is not otherwise available

    logging.info("  Getting weather data")
    weather_2019: Dict = mmw_run.get_project_weather(project_id, weather_layer)
    if weather_2019 is None or weather_2019 == {}:
        logging.info(
            "*** Couldn't get 2019 weather for {} ({})!! Will use older weather data.".format(
                huc_row["huc"], huc_row["name"]
            )
        )
    else:
        logging.info("  --Got weather data")
        used_weather_layer = weather_layer
        gwlfe_mods = [weather_2019["output"]]

    # clean up by deleting project
    logging.info("  Deleting project {}".format(project_id))
    mmw_run.delete_project(project_id)

    return used_weather_layer, gwlfe_mods


#%%
# create empty lists to hold results
gwlfe_monthlies = []
gwlfe_metas = []
gwlfe_summaries = []
gwlfe_whole_load_summaries = []
gwlfe_whole_source_loads = []
gwlfe_sb_load_summaries = []
gwlfe_sb_source_loads = []
srat_rates = []
srat_concs = []

#%%
for idx, huc_row in hucs_to_run.iterrows():
    logging.info("=====================")
    logging.info(
        "{} ({}) -- {} of {}".format(
            huc_row["huc"], huc_row["name"], idx, len(hucs_to_run.index)
        )
    )

    mapshed_job_label = "{}_{}".format(huc_row["huc"], land_use_layer)
    mapshed_payload = {
        "huc": huc_row["huc"],
        "layer_overrides": {
            "__LAND__": mmw_run.land_use_layers[land_use_layer],
            "__STREAMS__": stream_layer,
        },
    }

    mapshed_whole_job_id, closest_stations = read_or_run_mapshed(
        mmw_run.gwlfe_prepare_endpoint, mapshed_job_label, mapshed_payload
    )
    if mapshed_whole_job_id is None:
        logging.info("  MapShed failed, continuing to next HUC")
        continue

    gwlfe_whole_result = None
    gwlfe_whole_job_dict, gwlfe_whole_result = mmw_run.read_dumped_result(
        mmw_run.gwlfe_run_endpoint,
        "{}_{}_{}".format(huc_row["huc"], land_use_layer, weather_layer),
        json_dump_path
        + "{}_{}_{}_{}.json".format(
            huc_row["huc"],
            land_use_layer,
            "USEPA_1960_1990",
            mmw_run._pprint_endpoint(mmw_run.gwlfe_run_endpoint),
        ),
        "SummaryLoads",
    )

    gwlfe_sb_result = None
    gwlfe_sb_job_dict, gwlfe_sb_result = mmw_run.read_dumped_result(
        mmw_run.subbasin_run_endpoint,
        "{}_{}_{}".format(huc_row["huc"], land_use_layer, weather_layer),
        json_dump_path
        + "{}_{}_{}_{}.json".format(
            huc_row["huc"],
            land_use_layer,
            "USEPA_1960_1990",
            mmw_run._pprint_endpoint(mmw_run.subbasin_run_endpoint),
        ),
        "SummaryLoads",
    )
    if gwlfe_whole_result is None and gwlfe_sb_result is None:
        used_weather_layer, gwlfe_mods = get_weather_modifications(
            huc_row, mapshed_whole_job_id, mapshed_payload["layer_overrides"]
        )
        gwlfe_job_label = "{}_{}_{}".format(
            huc_row["huc"], land_use_layer, used_weather_layer
        )
    elif gwlfe_whole_job_dict is not None and gwlfe_whole_job_dict["payload"][
        "modifications"
    ] == [{}]:
        used_weather_layer = "USEPA_1960_1990"
    elif gwlfe_sb_job_dict is not None and gwlfe_sb_job_dict["payload"][
        "modifications"
    ] == [{}]:
        used_weather_layer = "USEPA_1960_1990"
    else:
        used_weather_layer = weather_layer

    if gwlfe_whole_result is None and mapshed_whole_job_id is not None:
        gwlfe_whole_job_dict, gwlfe_whole_result = run_gwlfe(
            mmw_run.gwlfe_run_endpoint,
            gwlfe_job_label,
            mapshed_whole_job_id,
            gwlfe_mods,
        )

    mapshed_sb_job_id, _ = read_or_run_mapshed(
        mmw_run.subbasin_prepare_endpoint, mapshed_job_label, mapshed_payload
    )

    if gwlfe_sb_result is None and mapshed_sb_job_id is not None:
        gwlfe_sb_job_dict, gwlfe_sb_result = run_gwlfe(
            mmw_run.subbasin_run_endpoint,
            gwlfe_job_label,
            mapshed_sb_job_id,
            gwlfe_mods,
        )

    logging.info("  Framing data")
    if gwlfe_whole_result is not None:
        gwlfe_monthly = pd.DataFrame(gwlfe_whole_result["monthly"])
        gwlfe_monthly["month"] = gwlfe_monthly.index + 1
        gwlfe_meta = pd.DataFrame(gwlfe_whole_result["meta"], index=[1])
        gwlfe_summary = pd.DataFrame(
            {
                key: gwlfe_whole_result[key]
                for key in ["AreaTotal", "MeanFlow", "MeanFlowPerSecond"]
            },
            index=[1],
        )
        gwlfe_whole_load_summary = pd.DataFrame(gwlfe_whole_result["SummaryLoads"])
        gwlfe_whole_sources = pd.DataFrame(gwlfe_whole_result["Loads"])

        for frame in [
            gwlfe_monthly,
            gwlfe_meta,
            gwlfe_summary,
            gwlfe_whole_load_summary,
            gwlfe_whole_sources,
        ]:
            frame["gwlfe_endpoint"] = "gwlfe"
            frame["huc_run"] = huc_row["huc"]
            frame["huc_run_level"] = huc_row["huc_level"]
            frame["huc"] = huc_row["huc"]
            frame["huc_name"] = huc_row["name"]
            frame["huc_states"] = huc_row["states"]
            frame["huc_areaacres"] = huc_row["areaacres"]
            frame["huc_level"] = huc_row["huc_level"]
            frame["land_use_source"] = land_use_layer
            frame["stream_layer"] = stream_layer
            frame["weather_source"] = used_weather_layer
            frame["closest_weather_stations"] = closest_stations
        gwlfe_monthlies.append(gwlfe_monthly)
        gwlfe_metas.append(gwlfe_meta)
        gwlfe_summaries.append(gwlfe_summary)
        gwlfe_whole_load_summaries.append(gwlfe_whole_load_summary)
        gwlfe_whole_source_loads.append(gwlfe_whole_sources)

    if gwlfe_sb_result is not None:
        for huc12 in gwlfe_sb_result["HUC12s"].keys():
            gwlfe_sb_load_summary = pd.DataFrame(
                gwlfe_sb_result["HUC12s"][huc12]["SummaryLoads"], index=[1]
            )
            gwlfe_sb_sources = pd.DataFrame(gwlfe_sb_result["HUC12s"][huc12]["Loads"])
            huc_srat_catchments = []
            for catchment in gwlfe_sb_result["HUC12s"][huc12]["Catchments"].keys():
                catch_frame = pd.DataFrame.from_dict(
                    gwlfe_sb_result["HUC12s"][huc12]["Catchments"][catchment],
                    orient="index",
                )
                catch_frame["catchment"] = catchment
                huc_srat_catchments.append(catch_frame)
            if len(huc_srat_catchments) > 0:
                huc_srat_catchments2 = pd.concat(
                    huc_srat_catchments, ignore_index=False
                )

            for frame in [
                gwlfe_sb_load_summary,
                gwlfe_sb_sources,
                huc_srat_catchments2,
            ]:
                frame["gwlfe_endpoint"] = "subbasin"
                frame["huc_run"] = huc_row["huc"]
                frame["huc_run_level"] = huc_row["huc_level"]
                frame["huc"] = huc12
                frame["huc_level"] = 12
                frame["land_use_source"] = land_use_layer
                frame["stream_layer"] = stream_layer
                frame["weather_source"] = used_weather_layer
                frame["closest_weather_stations"] = closest_stations
            gwlfe_sb_load_summaries.append(gwlfe_sb_load_summary)
            gwlfe_sb_source_loads.append(gwlfe_sb_sources)
            srat_rates.append(
                huc_srat_catchments2.loc[
                    huc_srat_catchments2.index == "TotalLoadingRates"
                ].copy()
            )
            srat_concs.append(
                huc_srat_catchments2.loc[
                    huc_srat_catchments2.index == "LoadingRateConcentrations"
                ].copy()
            )

#%% join various results and save csv's
gwlfe_monthly_results = pd.concat(gwlfe_monthlies, ignore_index=True)
gwlfe_monthly_results.sort_values(by=["huc"] + ["month"]).reset_index(drop=True).to_csv(
    csv_path + "gwlfe_whole_monthly_q" + csv_extension
)

gwlfe_metas_results = pd.concat(gwlfe_metas, ignore_index=True)
gwlfe_metas_results.sort_values(by=["huc"]).reset_index(drop=True).to_csv(
    csv_path + "gwlfe_whole_metadata" + csv_extension
)
gwlfe_sum_results = pd.concat(gwlfe_summaries, ignore_index=True)
gwlfe_sum_results.sort_values(by=["huc"]).reset_index(drop=True).to_csv(
    csv_path + "gwlfe_whole_summ_q" + csv_extension
)


gwlfe_whole_load_sum_results = pd.concat(gwlfe_whole_load_summaries, ignore_index=True)
gwlfe_whole_load_sum_results.sort_values(by=["huc"] + ["Source"]).reset_index(
    drop=True
).to_csv(csv_path + "gwlfe_whole_load_summaries" + csv_extension)

gwlfe_whole_source_load_results = pd.concat(gwlfe_whole_source_loads, ignore_index=True)
gwlfe_whole_source_load_results.sort_values(by=["huc"] + ["Source"]).reset_index(
    drop=True
).to_csv(csv_path + "gwlfe_whole_source_summaries" + csv_extension)


gwlfe_sb_load_sum_results = pd.concat(gwlfe_sb_load_summaries, ignore_index=True)
gwlfe_sb_load_sum_results.sort_values(by=["huc"] + ["Source"]).reset_index(
    drop=True
).to_csv(csv_path + "gwlfe_sb_load_summaries" + csv_extension)

gwlfe_sb_source_load_results = pd.concat(gwlfe_sb_source_loads, ignore_index=True)
gwlfe_sb_source_load_results.sort_values(by=["huc"] + ["Source"]).reset_index(
    drop=True
).to_csv(csv_path + "gwlfe_sb_source_summaries" + csv_extension)


srat_rate_results = pd.concat(srat_rates, ignore_index=True)
srat_rate_results.sort_values(by=["huc", "catchment"]).reset_index(drop=True).to_csv(
    csv_path + "srat_catchment_load_rates" + csv_extension
)

srat_conc_results = pd.concat(srat_concs, ignore_index=True)
srat_conc_results.sort_values(by=["huc", "catchment"]).reset_index(drop=True).to_csv(
    csv_path + "srat_catchment_concs" + csv_extension
)


#%%
logging.info("DONE!")

# %%