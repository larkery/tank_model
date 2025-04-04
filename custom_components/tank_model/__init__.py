"""
Hot water tank model
"""

DOMAIN = "hot_water_tank"

import math
import logging
import voluptuous as vol
from datetime import datetime, timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.helpers.config_validation as cv
from homeassistant.const import ( CONF_NAME )
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA

_LOGGER = logging.getLogger(__name__)

# Configuration parameters
CONF_LAYERS = "layers"
CONF_DIAMETER = "diameter_m"
CONF_HEIGHT = "height_m"
CONF_VOLUME = "volume_liters"
CONF_INLET_TEMP = "inlet_temperature"
CONF_AMBIENT_TEMP = "ambient_temperature"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_THERMOSTAT = "thermostat"
CONF_U_VALUE = "u_value"
CONF_USE_TEMP = "use_temperature"
CONF_HEATERS = "heaters"

# Service names
SERVICE_SET_HEATER_POWER = "set_heater_power"
SERVICE_USE_WATER = "use_water"
SERVICE_SET_STATE = "set_state"

# Service parameters
ATTR_POWER = "power_kw"
ATTR_VOLUME = "volume_liters"
ATTR_TEMPERATURES = "layer_temperature"

# Configuration defaults
DEFAULT_NAME = "Hot Water Tank"
DEFAULT_LAYERS = 20
DEFAULT_DIAMETER = 0.55  # meters
DEFAULT_HEIGHT = 1.3
DEFAULT_VOLUME = 180 
DEFAULT_INLET_TEMP = 15  # Celsius
DEFAULT_AMBIENT_TEMP = 20  # Celsius
DEFAULT_UPDATE_INTERVAL = 120  # seconds
DEFAULT_HEATERS = [0.1, 0.6]
DEFAULT_THERMOSTAT = 60
DEFAULT_U_VALUE = 0.5
DEFAULT_USE_TEMP = 45.0

# Schema for tank configuration
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_LAYERS, default=DEFAULT_LAYERS): cv.positive_int,
        vol.Optional(CONF_DIAMETER, default=DEFAULT_DIAMETER): vol.Coerce(float),
        vol.Optional(CONF_HEIGHT, default=DEFAULT_HEIGHT): vol.Coerce(float),
        vol.Optional(CONF_VOLUME, default=DEFAULT_VOLUME): vol.Coerce(float),
        vol.Optional(CONF_INLET_TEMP, default=DEFAULT_INLET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_AMBIENT_TEMP, default=DEFAULT_AMBIENT_TEMP): vol.Coerce(float),
        vol.Optional(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): vol.Coerce(int),
        vol.Optional(CONF_THERMOSTAT, default=DEFAULT_THERMOSTAT): vol.Coerce(float),
        vol.Optional(CONF_U_VALUE, default=DEFAULT_U_VALUE): vol.Coerce(float),
        vol.Optional(CONF_USE_TEMP, default=DEFAULT_USE_TEMP): vol.Coerce(float),
        vol.Optional(CONF_HEATERS, default=DEFAULT_HEATERS): vol.Schema([
            vol.Coerce(float)
        ])
    })
}, extra=vol.ALLOW_EXTRA)

# Schema for setting heater power
SET_HEATER_POWER_SCHEMA = vol.Schema({
    vol.Required(ATTR_POWER): vol.Coerce(float)
})

# Schema for drawing water
USE_WATER_SCHEMA = vol.Schema({
    vol.Required(ATTR_VOLUME): vol.Coerce(float)
})

SET_STATE_SCHEMA = vol.Schema({
    vol.Required(ATTR_TEMPERATURES): vol.Schema([vol.Coerce(float)])
})


class Tank:
    def __init__(self,
                 diameter=0.55,
                 height=1.3,
                 volume=180,
                 heater_layers = [0, 4],
                 thermostat = 60.0,
                 layers = 10,
                 u_value = 0.5,
                 inlet_temperature = 15,
                 ambient_temperature = 20):
        self.diameter = diameter
        self.height = height
        self.volume = volume
        self.heater_layers = heater_layers
        self.state = [inlet_temperature] * layers
        self.inlet_temperature = inlet_temperature
        self.ambient_temperature = ambient_temperature
        self.thermostat = thermostat
        self.heating_power = 0
        self.u_value = u_value
        self.heating = None

    def update(self, time):
        if time <= 0: return
        n_layers = len(self.state)
        
        layer_height = self.height / n_layers
        horiz_area = math.pi * self.diameter * layer_height
        top_area = math.pi * ((self.diameter / 2) ** 2)

        new_state = self.state.copy()
        slice_volume = self.volume / n_layers
        self.heating = None

        # determine if heater is on - highest heater wins
        heating_layer = None
        for i in self.heater_layers:
            if new_state[i] < self.thermostat:
                heating_layer = max(heating_layer or i, i)
        
        for i in range(n_layers):
            delta_t = self.state[i] - self.ambient_temperature
            slice_area = horiz_area
            if i == 0 or i == n_layers - 1:
                slice_area = slice_area + top_area
            loss = self.u_value * slice_area * delta_t
            conduction = 0
            if i > 0:
                conduction += 0.6 * (self.state[i-1] - self.state[i])
            if i < n_layers - 1:
                conduction += 0.6 * (self.state[i+1] - self.state[i])
            if i == heating_layer:
                heat_in = self.heating_power
                self.heating = heating_layer if heat_in > 0 else None
            else:
                heat_in = 0
            power = heat_in + conduction - loss
            energy = power * time
            slice_delta_t = energy * 0.00024 / slice_volume
            new_state[i] += slice_delta_t

        # convection. not exactly fluid dynamics this has no time
        # dimension so probably has something wrong about scaling
        # in it.
        out_of_order = True
        while out_of_order:
            for i in range(n_layers-1):
                if new_state[i] > (0.001+new_state[i+1]):
                    hi = new_state[i]    - 0.4 * (new_state[i] - new_state[i+1])
                    lo = new_state[i+1]  + 0.4 * (new_state[i] - new_state[i+1])
                    new_state[i] = lo
                    new_state[i+1] = hi
                    out_of_order = True
            else:
                out_of_order = False

        new_state = [min(self.thermostat + 5.0, x) for x in new_state]
                
        self.state = new_state

    def available_volume(self, target_temperature):
        acc = 0
        n_layers = len(self.state)
        slice_volume = self.volume / n_layers
        for layer_temp in self.state:
            if layer_temp >= target_temperature:
                # mixing produces
                # temp = (temp_hot * vol_hot + temp_cold * vol_cold) / (vol_hot + vol_cold)
                # vol_hot is fixed so find vol_cold
                # vh * temp + vc * temp = th * vh + tc * vc
                # vc * temp - vc * tc = vh * th - vh * temp
                # vc * (temp - tc) = vh * (th - temp)
                # vc = [vh * (th - temp)] / (temp - tc)
                vc = slice_volume * (layer_temp - target_temperature) / (target_temperature - self.inlet_temperature)
                acc += vc + slice_volume
        return acc
        

    def use_water(self, volume_l, target_temperature):
        """
        Update self.state with the new layer temperatures after volume_l
        liters of water has been produced at target_temperature, by mixing water
        taken from the top (last element) of the tank with water at the inlet temperature (at the tap).

        The hot water layers in the tank move up as they are used, and are replaced with water at inlet_temperature from the bottom (first) layer.

        If the volume cannot be produced at the target temperature, because the tank does not contain enough hot water, assume that hot water is produced up to volume_l at the highest temperature possible below target_temperature
        """
        if volume_l <= 0: return

        n_layers = len(self.state)
        slice_volume = self.volume / n_layers

        used_volume = 0
        
        for i in reversed(range(n_layers)):
            layer_temp = self.state[i]
            # how much can we make with this layer
            vc = slice_volume * (layer_temp - target_temperature) / \
                (target_temperature - self.inlet_temperature) \
                if layer_temp >= target_temperature else 0
            vt = vc + slice_volume
            if volume_l >= vt:
                volume_l -= vt
                used_volume += slice_volume
            else:
                used_volume += slice_volume * volume_l / vt
                volume_l = 0
                break

        # discard the slices we used all of
        whole_slices = int(used_volume // slice_volume)
        
        # mix the slice we used partially if any
        fill = used_volume % slice_volume

        _LOGGER.warning(f"{used_volume} used which is {whole_slices} slices and {fill} left")

        new_state = self.state[:n_layers - whole_slices]
        if fill > 0:
           keep = slice_volume - fill
           t_below = self.inlet_temperature
           for i in range(len(new_state)):
               t_here = (new_state[i]*keep + t_below * fill) / (keep + fill)
               t_below = new_state[i]
               new_state[i] = t_here
        new_state = ([self.inlet_temperature] * whole_slices) + new_state
        self.state = new_state

class HotWaterTankEntity(RestoreEntity, Entity):
    def __init__(self,
                 name,
                 layers,
                 diameter,
                 height,
                 volume,
                 inlet_temp,
                 ambient_temp,
                 thermostat_temp,
                 u_value,
                 use_temp,
                 heater_heights):
        self.entity_id = f"sensor.{name.lower().replace(' ', '_')}_available_volume"
        self._name = name
        _LOGGER.warning(f"h: {heater_heights}")
        self._model = Tank(diameter = diameter,
                           height = height,
                           layers = layers,
                           heater_layers = [int(layers * h / height)
                                            for h in heater_heights],
                           u_value = u_value,
                           inlet_temperature = inlet_temp,
                           ambient_temperature = ambient_temp,
                           thermostat = thermostat_temp)
        self._last_update = datetime.now()
        self._use_temp = use_temp
        self._state = None

    def update(self):
        now = datetime.now()
        if type(self._last_update) == str:
            try: 
                self._last_update = datetime.strptime(
                    self._last_update,
                    '%Y-%m-%dT%H:%M:%S')
            except:
                self._last_update = now
        dt = (now - self._last_update).total_seconds()
        self._model.update(dt)
        self._last_update = now
        self._state = round(self._model.available_volume(self._use_temp))

    def set_heater_power(self, power_kw):
        self._model.heating_power = power_kw * 1000.0
        self.update()

    def use_water(self, volume_l):
        self._model.use_water(volume_l, self._use_temp)
        self.update()
        
    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return "L"  # Liters
    
    @property
    def state_attributes(self):
        # we don't store heater power
        return {
            "temperatures": [round(temp, 1) for temp in self._model.state],
            "last_model_update": self._last_update.strftime('%Y-%m-%dT%H:%M:%S'),
            "heater_layers": self._model.heater_layers,
            "heating_layer": self._model.heating,
            "heating_power": self._model.heating_power
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        
        if state:
            self._model.state = list(map(float, state.attributes.get('temperatures',
                                                                     self._model.state)))
            self._last_update = state.attributes.get('last_model_update', datetime.now())
            if type(self._last_update) == str:
                self._last_update = datetime.strptime(self._last_update,
                                                      '%Y-%m-%dT%H:%M:%S')

        self.update()

async def async_setup(hass, config):
    if DOMAIN not in config:
        return True

    config = config[DOMAIN]

    entity = HotWaterTankEntity(
        config[CONF_NAME],
        config[CONF_LAYERS],
        config[CONF_DIAMETER],
        config[CONF_HEIGHT],
        config[CONF_VOLUME],
        config[CONF_INLET_TEMP],
        config[CONF_AMBIENT_TEMP],
        config[CONF_THERMOSTAT],
        config[CONF_U_VALUE],
        config[CONF_USE_TEMP],
        config[CONF_HEATERS]
    )

    component = EntityComponent(_LOGGER, DOMAIN, hass)

    await component.async_add_entities([entity])

    async def _update_tank(_ignore):
        entity.update()
        await entity.async_update_ha_state()

    update_interval = config[CONF_UPDATE_INTERVAL]
    async_track_time_interval(hass, _update_tank, timedelta(seconds=update_interval))

    async def async_handle_set_heater_power(call):
        power = call.data.get(ATTR_POWER, 0)
        entity.set_heater_power(power)
        await entity.async_update_ha_state()

    async def async_handle_use_water(call):
        volume = call.data.get(ATTR_VOLUME, 0)
        entity.use_water(volume)
        await entity.async_update_ha_state()

    async def async_handle_set_state(call):
        temps = call.data.get(ATTR_TEMPERATURES, [])
        if temps:
            l0 = len(entity._model.state)
            l1 = len(temps)
            r = l1/l0

            
            entity._model.state = temps
            entity._model.heater_layers = [int(r * h)
                                            for h in entity._model.heater_layers]
            
            entity._last_update = datetime.now()
        await entity.async_update_ha_state()

    hass.services.async_register(
        DOMAIN, 
        SERVICE_SET_HEATER_POWER, 
        async_handle_set_heater_power,
        schema=SET_HEATER_POWER_SCHEMA
    )
    
    hass.services.async_register(
        DOMAIN, 
        SERVICE_USE_WATER, 
        async_handle_use_water,
        schema=USE_WATER_SCHEMA
    )

    hass.services.async_register(
        DOMAIN, 
        SERVICE_SET_STATE, 
        async_handle_set_state,
        schema=SET_STATE_SCHEMA
    )


    return True
