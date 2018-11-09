#!/usr/bin/python3 -i
#
# Copyright (c) 2015-2016 Valve Corporation
# Copyright (c) 2015-2016 LunarG, Inc.
# Copyright (c) 2015-2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Tobin Ehlis <tobine@google.com>
# Author: Mark Lobodzinski <mark@lunarg.com>
#
# This script generates the dispatch portion of a factory layer which intercepts
# all Vulkan  functions. The resultant factory layer allows rapid development of
# layers and interceptors.

import os,re,sys
from generator import *
from common_codegen import *

# LayerFactoryGeneratorOptions - subclass of GeneratorOptions.
#
# Adds options used by LayerFactoryOutputGenerator objects during factory
# layer generation.
#
# Additional members
#   prefixText - list of strings to prefix generated header with
#     (usually a copyright statement + calling convention macros).
#   protectFile - True if multiple inclusion protection should be
#     generated (based on the filename) around the entire header.
#   protectFeature - True if #ifndef..#endif protection should be
#     generated around a feature interface in the header file.
#   genFuncPointers - True if function pointer typedefs should be
#     generated
#   protectProto - If conditional protection should be generated
#     around prototype declarations, set to either '#ifdef'
#     to require opt-in (#ifdef protectProtoStr) or '#ifndef'
#     to require opt-out (#ifndef protectProtoStr). Otherwise
#     set to None.
#   protectProtoStr - #ifdef/#ifndef symbol to use around prototype
#     declarations, if protectProto is set
#   apicall - string to use for the function declaration prefix,
#     such as APICALL on Windows.
#   apientry - string to use for the calling convention macro,
#     in typedefs, such as APIENTRY.
#   apientryp - string to use for the calling convention macro
#     in function pointer typedefs, such as APIENTRYP.
#   indentFuncProto - True if prototype declarations should put each
#     parameter on a separate line
#   indentFuncPointer - True if typedefed function pointers should put each
#     parameter on a separate line
#   alignFuncParam - if nonzero and parameters are being put on a
#     separate line, align parameter names at the specified column
class UberLayerGeneratorOptions(GeneratorOptions):
    def __init__(self,
                 filename = None,
                 directory = '.',
                 apiname = None,
                 profile = None,
                 versions = '.*',
                 emitversions = '.*',
                 defaultExtensions = None,
                 addExtensions = None,
                 removeExtensions = None,
                 emitExtensions = None,
                 sortProcedure = regSortFeatures,
                 prefixText = "",
                 genFuncPointers = True,
                 protectFile = True,
                 protectFeature = True,
                 apicall = '',
                 apientry = '',
                 apientryp = '',
                 indentFuncProto = True,
                 indentFuncPointer = False,
                 alignFuncParam = 0,
                 helper_file_type = '',
                 expandEnumerants = True):
        GeneratorOptions.__init__(self, filename, directory, apiname, profile,
                                  versions, emitversions, defaultExtensions,
                                  addExtensions, removeExtensions, emitExtensions, sortProcedure)
        self.prefixText      = prefixText
        self.genFuncPointers = genFuncPointers
        self.protectFile     = protectFile
        self.protectFeature  = protectFeature
        self.apicall         = apicall
        self.apientry        = apientry
        self.apientryp       = apientryp
        self.indentFuncProto = indentFuncProto
        self.indentFuncPointer = indentFuncPointer
        self.alignFuncParam  = alignFuncParam

# UberLayerOutputGenerator - subclass of OutputGenerator.
# Generates a LayerFactory layer that intercepts all API entrypoints
#  This is intended to be used as a starting point for creating custom layers
#
# ---- methods ----
# UberLayerOutputGenerator(errFile, warnFile, diagFile) - args as for
#   OutputGenerator. Defines additional internal state.
# ---- methods overriding base class ----
# beginFile(genOpts)
# endFile()
# beginFeature(interface, emit)
# endFeature()
# genType(typeinfo,name)
# genStruct(typeinfo,name)
# genGroup(groupinfo,name)
# genEnum(enuminfo, name)
# genCmd(cmdinfo)
class UberLayerOutputGenerator(OutputGenerator):
    """Generate specified API interfaces in a specific style, such as a C header"""
    # This is an ordered list of sections in the header file.
    TYPE_SECTIONS = ['include', 'define', 'basetype', 'handle', 'enum',
                     'group', 'bitmask', 'funcpointer', 'struct']
    ALL_SECTIONS = TYPE_SECTIONS + ['command']

    precallvalidate_loop = "for (auto intercept : layer_data->object_dispatch) {"
    precallrecord_loop = precallvalidate_loop
    # TODO: This loop should run backwards!
    postcallrecord_loop = "for (auto intercept : layer_data->object_dispatch) {"

    inline_custom_header_preamble = """
#define NOMINMAX
#include <mutex>
#include <cinttypes>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unordered_map>
#include <unordered_set>
#include <algorithm>
#include <memory>

#include "vk_loader_platform.h"
#include "vulkan/vulkan.h"
#include "vk_layer_config.h"
#include "vk_layer_data.h"
#include "vk_layer_logging.h"
#include "vk_object_types.h"
#include "vulkan/vk_layer.h"
#include "vk_enum_string_helper.h"
#include "vk_layer_extension_utils.h"
#include "vk_layer_utils.h"
#include "vulkan/vk_layer.h"
#include "vk_dispatch_table_helper.h"
#include "vk_validation_error_messages.h"
#include "vk_extension_helper.h"
#include "vk_safe_struct.h"

extern uint64_t global_unique_id;
extern std::unordered_map<uint64_t, uint64_t> unique_id_mapping;

// TODO: This variable controls handle wrapping -- in the future it should be hooked
//       up to the new VALIDATION_FEATURES extension. Temporarily, control with a compile-time flag.
#if defined(UBER_LAYER_CAN_WRAP_HANDLES)
bool wrap_handles = TRUE;
#else
const bool wrap_handles = FALSE;
#endif

"""

    inline_custom_header_class_definition = """

// Layer object type identifiers
enum LayerObjectTypeId {
    LayerObjectTypeThreading,
    LayerObjectTypeParameterValidation,
    LayerObjectTypeObjectTracker,
    LayerObjectTypeCoreValidation,
};

struct TEMPLATE_STATE {
    VkDescriptorUpdateTemplateKHR desc_update_template;
    safe_VkDescriptorUpdateTemplateCreateInfo create_info;

    TEMPLATE_STATE(VkDescriptorUpdateTemplateKHR update_template, safe_VkDescriptorUpdateTemplateCreateInfo *pCreateInfo)
        : desc_update_template(update_template), create_info(*pCreateInfo) {}
};

// Uber Layer validation object base class definition
class ValidationObject {
    public:
        uint32_t api_version;
        debug_report_data* report_data = nullptr;
        std::vector<VkDebugReportCallbackEXT> logging_callback;
        std::vector<VkDebugUtilsMessengerEXT> logging_messenger;

        VkLayerInstanceDispatchTable instance_dispatch_table;
        VkLayerDispatchTable device_dispatch_table;

        InstanceExtensions instance_extensions;
        DeviceExtensions device_extensions = {};

        VkInstance instance = VK_NULL_HANDLE;
        VkPhysicalDevice physical_device = VK_NULL_HANDLE;
        VkDevice device = VK_NULL_HANDLE;

        std::vector<ValidationObject*> object_dispatch;
        LayerObjectTypeId container_type;

        // Constructor
        ValidationObject(){};
        // Destructor
        virtual ~ValidationObject() {};

        std::string layer_name = "UBER";

        // Handle Wrapping Data
        // Reverse map display handles
        std::unordered_map<VkDisplayKHR, uint64_t> display_id_reverse_mapping;
        std::unordered_map<uint64_t, std::unique_ptr<TEMPLATE_STATE>> desc_template_map;
        std::unordered_set<std::string> device_extension_set;
        struct SubpassesUsageStates {
            std::unordered_set<uint32_t> subpasses_using_color_attachment;
            std::unordered_set<uint32_t> subpasses_using_depthstencil_attachment;
        };
        // Uses unwrapped handles
        std::unordered_map<VkRenderPass, SubpassesUsageStates> renderpasses_states;
        // Map of wrapped swapchain handles to arrays of wrapped swapchain image IDs
        // Each swapchain has an immutable list of wrapped swapchain image IDs -- always return these IDs if they exist
        std::unordered_map<VkSwapchainKHR, std::vector<VkImage>> swapchain_wrapped_image_handle_map;


        // Unwrap a handle.  Must hold lock.
        template <typename HandleType>
        HandleType Unwrap(HandleType wrappedHandle) {
            // TODO: don't use operator[] here.
            return (HandleType)unique_id_mapping[reinterpret_cast<uint64_t const &>(wrappedHandle)];
        }

        // Wrap a newly created handle with a new unique ID, and return the new ID -- must hold lock.
        template <typename HandleType>
        HandleType WrapNew(HandleType newlyCreatedHandle) {
            auto unique_id = global_unique_id++;
            unique_id_mapping[unique_id] = reinterpret_cast<uint64_t const &>(newlyCreatedHandle);
            return (HandleType)unique_id;
        }

        // Specialized handling for VkDisplayKHR. Adds an entry to enable reverse-lookup. Must hold lock.
        VkDisplayKHR WrapDisplay(VkDisplayKHR newlyCreatedHandle, ValidationObject *map_data) {
            auto unique_id = global_unique_id++;
            unique_id_mapping[unique_id] = reinterpret_cast<uint64_t const &>(newlyCreatedHandle);
            map_data->display_id_reverse_mapping[newlyCreatedHandle] = unique_id;
            return (VkDisplayKHR)unique_id;
        }

        // VkDisplayKHR objects don't have a single point of creation, so we need to see if one already exists in the map before
        // creating another. Must hold lock.
        VkDisplayKHR MaybeWrapDisplay(VkDisplayKHR handle, ValidationObject *map_data) {
            // See if this display is already known
            auto it = map_data->display_id_reverse_mapping.find(handle);
            if (it != map_data->display_id_reverse_mapping.end()) return (VkDisplayKHR)it->second;
            // Unknown, so wrap
            return WrapDisplay(handle, map_data);
        }

        // Pre/post hook point declarations
"""

    inline_copyright_message = """
// This file is ***GENERATED***.  Do Not Edit.
// See uber_layer_generator.py for modifications.

/* Copyright (c) 2015-2018 The Khronos Group Inc.
 * Copyright (c) 2015-2018 Valve Corporation
 * Copyright (c) 2015-2018 LunarG, Inc.
 * Copyright (c) 2015-2018 Google Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Author: Mark Lobodzinski <mark@lunarg.com>
 */"""

    inline_custom_source_preamble = """

#include <string.h>
#include <mutex>

#define VALIDATION_ERROR_MAP_IMPL

#include "uber_layer.h"
#include "uber_layer_dispatch.h"

std::unordered_map<void*, ValidationObject*> layer_data_map;

// Global unique object identifier.  All increments must be guarded by global_lock.
uint64_t global_unique_id = 1;
// Map uniqueID to actual object handle
std::unordered_map<uint64_t, uint64_t> unique_id_mapping;

// Include child object (layer) definitions
#include "object_lifetime_validation.h"

using mutex_t = std::mutex;
using lock_guard_t = std::lock_guard<mutex_t>;
using unique_lock_t = std::unique_lock<mutex_t>;

namespace vulkan_uber_layer {

using std::unordered_map;

mutex_t global_lock;

static const VkLayerProperties global_layer = {
    "VK_LAYER_KHRONOS_uber_layer", VK_LAYER_API_VERSION, 1, "LunarG Ultra Layer",
};

static const VkExtensionProperties instance_extensions[] = {{VK_EXT_DEBUG_REPORT_EXTENSION_NAME, VK_EXT_DEBUG_REPORT_SPEC_VERSION}};

extern const std::unordered_map<std::string, void*> name_to_funcptr_map;


// Manually written functions

// Check enabled instance extensions against supported instance extension whitelist
static void InstanceExtensionWhitelist(ValidationObject *layer_data, const VkInstanceCreateInfo *pCreateInfo, VkInstance instance) {
    for (uint32_t i = 0; i < pCreateInfo->enabledExtensionCount; i++) {
        // Check for recognized instance extensions
        if (!white_list(pCreateInfo->ppEnabledExtensionNames[i], kInstanceExtensionNames)) {
            log_msg(layer_data->report_data, VK_DEBUG_REPORT_ERROR_BIT_EXT, VK_DEBUG_REPORT_OBJECT_TYPE_UNKNOWN_EXT, 0,
                    kVUIDUndefined,
                    "Instance Extension %s is not supported by this layer.  Using this extension may adversely affect validation "
                    "results and/or produce undefined behavior.",
                    pCreateInfo->ppEnabledExtensionNames[i]);
        }
    }
}

// Check enabled device extensions against supported device extension whitelist
static void DeviceExtensionWhitelist(ValidationObject *layer_data, const VkDeviceCreateInfo *pCreateInfo, VkDevice device) {
    for (uint32_t i = 0; i < pCreateInfo->enabledExtensionCount; i++) {
        // Check for recognized device extensions
        if (!white_list(pCreateInfo->ppEnabledExtensionNames[i], kDeviceExtensionNames)) {
            log_msg(layer_data->report_data, VK_DEBUG_REPORT_ERROR_BIT_EXT, VK_DEBUG_REPORT_OBJECT_TYPE_UNKNOWN_EXT, 0,
                    kVUIDUndefined,
                    "Device Extension %s is not supported by this layer.  Using this extension may adversely affect validation "
                    "results and/or produce undefined behavior.",
                    pCreateInfo->ppEnabledExtensionNames[i]);
        }
    }
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL GetDeviceProcAddr(VkDevice device, const char *funcName) {
    auto layer_data = GetLayerDataPtr(get_dispatch_key(device), layer_data_map);
    const auto &item = name_to_funcptr_map.find(funcName);
    if (item != name_to_funcptr_map.end()) {
        return reinterpret_cast<PFN_vkVoidFunction>(item->second);
    }
    auto &table = layer_data->device_dispatch_table;
    if (!table.GetDeviceProcAddr) return nullptr;
    return table.GetDeviceProcAddr(device, funcName);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL GetInstanceProcAddr(VkInstance instance, const char *funcName) {
    const auto &item = name_to_funcptr_map.find(funcName);
    if (item != name_to_funcptr_map.end()) {
        return reinterpret_cast<PFN_vkVoidFunction>(item->second);
    }
    auto layer_data = GetLayerDataPtr(get_dispatch_key(instance), layer_data_map);
    auto &table = layer_data->instance_dispatch_table;
    if (!table.GetInstanceProcAddr) return nullptr;
    return table.GetInstanceProcAddr(instance, funcName);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL GetPhysicalDeviceProcAddr(VkInstance instance, const char *funcName) {
    auto layer_data = GetLayerDataPtr(get_dispatch_key(instance), layer_data_map);
    auto &table = layer_data->instance_dispatch_table;
    if (!table.GetPhysicalDeviceProcAddr) return nullptr;
    return table.GetPhysicalDeviceProcAddr(instance, funcName);
}

VKAPI_ATTR VkResult VKAPI_CALL EnumerateInstanceLayerProperties(uint32_t *pCount, VkLayerProperties *pProperties) {
    return util_GetLayerProperties(1, &global_layer, pCount, pProperties);
}

VKAPI_ATTR VkResult VKAPI_CALL EnumerateDeviceLayerProperties(VkPhysicalDevice physicalDevice, uint32_t *pCount,
                                                              VkLayerProperties *pProperties) {
    return util_GetLayerProperties(1, &global_layer, pCount, pProperties);
}

VKAPI_ATTR VkResult VKAPI_CALL EnumerateInstanceExtensionProperties(const char *pLayerName, uint32_t *pCount,
                                                                    VkExtensionProperties *pProperties) {
    if (pLayerName && !strcmp(pLayerName, global_layer.layerName))
        return util_GetExtensionProperties(1, instance_extensions, pCount, pProperties);

    return VK_ERROR_LAYER_NOT_PRESENT;
}

VKAPI_ATTR VkResult VKAPI_CALL EnumerateDeviceExtensionProperties(VkPhysicalDevice physicalDevice, const char *pLayerName,
                                                                  uint32_t *pCount, VkExtensionProperties *pProperties) {
    if (pLayerName && !strcmp(pLayerName, global_layer.layerName)) return util_GetExtensionProperties(0, NULL, pCount, pProperties);
    assert(physicalDevice);
    auto layer_data = GetLayerDataPtr(get_dispatch_key(physicalDevice), layer_data_map);
    return layer_data->instance_dispatch_table.EnumerateDeviceExtensionProperties(physicalDevice, NULL, pCount, pProperties);
}

VKAPI_ATTR VkResult VKAPI_CALL CreateInstance(const VkInstanceCreateInfo *pCreateInfo, const VkAllocationCallbacks *pAllocator,
                                              VkInstance *pInstance) {
    VkLayerInstanceCreateInfo* chain_info = get_chain_info(pCreateInfo, VK_LAYER_LINK_INFO);

    assert(chain_info->u.pLayerInfo);
    PFN_vkGetInstanceProcAddr fpGetInstanceProcAddr = chain_info->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkCreateInstance fpCreateInstance = (PFN_vkCreateInstance)fpGetInstanceProcAddr(NULL, "vkCreateInstance");
    if (fpCreateInstance == NULL) return VK_ERROR_INITIALIZATION_FAILED;
    chain_info->u.pLayerInfo = chain_info->u.pLayerInfo->pNext;

    // Create temporary dispatch vector for pre-calls until instance is created
    std::vector<ValidationObject*> local_object_dispatch;
#if BUILD_OBJECT_TRACKER
    auto object_tracker = new ObjectLifetimes;
    local_object_dispatch.emplace_back(object_tracker);
    object_tracker->container_type = LayerObjectTypeObjectTracker;
#endif


    // Init dispatch array and call registration functions
    for (auto intercept : local_object_dispatch) {
        intercept->PreCallValidateCreateInstance(pCreateInfo, pAllocator, pInstance);
    }
    for (auto intercept : local_object_dispatch) {
        intercept->PreCallRecordCreateInstance(pCreateInfo, pAllocator, pInstance);
    }

    VkResult result = fpCreateInstance(pCreateInfo, pAllocator, pInstance);
    if (result != VK_SUCCESS) return result;

    auto framework = GetLayerDataPtr(get_dispatch_key(*pInstance), layer_data_map);

    framework->object_dispatch = local_object_dispatch;

    framework->instance = *pInstance;
    layer_init_instance_dispatch_table(*pInstance, &framework->instance_dispatch_table, fpGetInstanceProcAddr);
    framework->report_data = debug_utils_create_instance(&framework->instance_dispatch_table, *pInstance, pCreateInfo->enabledExtensionCount,
                                                         pCreateInfo->ppEnabledExtensionNames);
    framework->api_version = framework->instance_extensions.InitFromInstanceCreateInfo(
        (pCreateInfo->pApplicationInfo ? pCreateInfo->pApplicationInfo->apiVersion : VK_API_VERSION_1_0), pCreateInfo);
#if BUILD_OBJECT_TRACKER
    layer_debug_report_actions(framework->report_data, framework->logging_callback, pAllocator, "lunarg_object_tracker");
    layer_debug_messenger_actions(framework->report_data, framework->logging_messenger, pAllocator, "lunarg_object_tracker");
#else
    layer_debug_report_actions(framework->report_data, framework->logging_callback, pAllocator, "lunarg_unique_objects");
    layer_debug_messenger_actions(framework->report_data, framework->logging_messenger, pAllocator, "lunarg_unique_objects");
#endif

    for (auto intercept : framework->object_dispatch) {
        intercept->PostCallRecordCreateInstance(pCreateInfo, pAllocator, pInstance);
    }

    InstanceExtensionWhitelist(framework, pCreateInfo, *pInstance);

    return result;
}

VKAPI_ATTR void VKAPI_CALL DestroyInstance(VkInstance instance, const VkAllocationCallbacks *pAllocator) {
    dispatch_key key = get_dispatch_key(instance);
    auto layer_data = GetLayerDataPtr(key, layer_data_map);
    """ + precallvalidate_loop + """
        intercept->PreCallValidateDestroyInstance(instance, pAllocator);
    }
    """ + precallrecord_loop + """
        intercept->PreCallRecordDestroyInstance(instance, pAllocator);
    }

    layer_data->instance_dispatch_table.DestroyInstance(instance, pAllocator);

    lock_guard_t lock(global_lock);
    """ + postcallrecord_loop + """
        intercept->PostCallRecordDestroyInstance(instance, pAllocator);
    }
    // Clean up logging callback, if any
    while (layer_data->logging_messenger.size() > 0) {
        VkDebugUtilsMessengerEXT messenger = layer_data->logging_messenger.back();
        layer_destroy_messenger_callback(layer_data->report_data, messenger, pAllocator);
        layer_data->logging_messenger.pop_back();
    }
    while (layer_data->logging_callback.size() > 0) {
        VkDebugReportCallbackEXT callback = layer_data->logging_callback.back();
        layer_destroy_report_callback(layer_data->report_data, callback, pAllocator);
        layer_data->logging_callback.pop_back();
    }

    layer_debug_utils_destroy_instance(layer_data->report_data);

    FreeLayerDataPtr(key, layer_data_map);
}

VKAPI_ATTR VkResult VKAPI_CALL CreateDevice(VkPhysicalDevice gpu, const VkDeviceCreateInfo *pCreateInfo,
                                            const VkAllocationCallbacks *pAllocator, VkDevice *pDevice) {
    unique_lock_t lock(global_lock);

    VkLayerDeviceCreateInfo *chain_info = get_chain_info(pCreateInfo, VK_LAYER_LINK_INFO);

    auto instance_interceptor = GetLayerDataPtr(get_dispatch_key(gpu), layer_data_map);

    PFN_vkGetInstanceProcAddr fpGetInstanceProcAddr = chain_info->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr fpGetDeviceProcAddr = chain_info->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    PFN_vkCreateDevice fpCreateDevice = (PFN_vkCreateDevice)fpGetInstanceProcAddr(instance_interceptor->instance, "vkCreateDevice");
    if (fpCreateDevice == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    chain_info->u.pLayerInfo = chain_info->u.pLayerInfo->pNext;

    for (auto intercept : instance_interceptor->object_dispatch) {
        intercept->PreCallValidateCreateDevice(gpu, pCreateInfo, pAllocator, pDevice);
    }
    for (auto intercept : instance_interceptor->object_dispatch) {
        intercept->PreCallRecordCreateDevice(gpu, pCreateInfo, pAllocator, pDevice);
    }
    lock.unlock();

    VkResult result = fpCreateDevice(gpu, pCreateInfo, pAllocator, pDevice);
    if (result != VK_SUCCESS) {
        return result;
    }

    lock.lock();
    auto device_interceptor = GetLayerDataPtr(get_dispatch_key(*pDevice), layer_data_map);
    layer_init_device_dispatch_table(*pDevice, &device_interceptor->device_dispatch_table, fpGetDeviceProcAddr);
    device_interceptor->device = *pDevice;
    device_interceptor->physical_device = gpu;
    device_interceptor->instance = instance_interceptor->instance;
    device_interceptor->report_data = layer_debug_utils_create_device(instance_interceptor->report_data, *pDevice);
    device_interceptor->api_version = instance_interceptor->api_version;

#if BUILD_OBJECT_TRACKER
    // Create child layer objects for this key and add to dispatch vector
    auto object_tracker = new ObjectLifetimes;
    // TODO:  Initialize child objects with parent info thru constuctor taking a parent object
    object_tracker->container_type = LayerObjectTypeObjectTracker;
    object_tracker->physical_device = gpu;
    object_tracker->instance = instance_interceptor->instance;
    object_tracker->report_data = device_interceptor->report_data;
    object_tracker->device_dispatch_table = device_interceptor->device_dispatch_table;
    device_interceptor->object_dispatch.emplace_back(object_tracker);
#endif

    for (auto intercept : instance_interceptor->object_dispatch) {
        intercept->PostCallRecordCreateDevice(gpu, pCreateInfo, pAllocator, pDevice);
    }
    lock.unlock();

    DeviceExtensionWhitelist(device_interceptor, pCreateInfo, *pDevice);

    return result;
}

VKAPI_ATTR void VKAPI_CALL DestroyDevice(VkDevice device, const VkAllocationCallbacks *pAllocator) {
    dispatch_key key = get_dispatch_key(device);
    auto layer_data = GetLayerDataPtr(key, layer_data_map);
    unique_lock_t lock(global_lock);
    """ + precallvalidate_loop + """
        intercept->PreCallValidateDestroyDevice(device, pAllocator);
    }
    """ + precallrecord_loop + """
        intercept->PreCallRecordDestroyDevice(device, pAllocator);
    }
    layer_debug_utils_destroy_device(device);
    lock.unlock();

    layer_data->device_dispatch_table.DestroyDevice(device, pAllocator);

    lock.lock();
    """ + postcallrecord_loop + """
        intercept->PostCallRecordDestroyDevice(device, pAllocator);
    }

    FreeLayerDataPtr(key, layer_data_map);
}

VKAPI_ATTR VkResult VKAPI_CALL CreateDebugReportCallbackEXT(VkInstance instance,
                                                            const VkDebugReportCallbackCreateInfoEXT *pCreateInfo,
                                                            const VkAllocationCallbacks *pAllocator,
                                                            VkDebugReportCallbackEXT *pCallback) {
    auto layer_data = GetLayerDataPtr(get_dispatch_key(instance), layer_data_map);
    """ + precallvalidate_loop + """
        intercept->PreCallValidateCreateDebugReportCallbackEXT(instance, pCreateInfo, pAllocator, pCallback);
    }
    """ + precallrecord_loop + """
        intercept->PreCallRecordCreateDebugReportCallbackEXT(instance, pCreateInfo, pAllocator, pCallback);
    }
    VkResult result = DispatchCreateDebugReportCallbackEXT(layer_data, instance, pCreateInfo, pAllocator, pCallback);
    result = layer_create_report_callback(layer_data->report_data, false, pCreateInfo, pAllocator, pCallback);
    """ + postcallrecord_loop + """
        intercept->PostCallRecordCreateDebugReportCallbackEXT(instance, pCreateInfo, pAllocator, pCallback);
    }
    return result;
}

VKAPI_ATTR void VKAPI_CALL DestroyDebugReportCallbackEXT(VkInstance instance, VkDebugReportCallbackEXT callback,
                                                         const VkAllocationCallbacks *pAllocator) {
    auto layer_data = GetLayerDataPtr(get_dispatch_key(instance), layer_data_map);
    """ + precallvalidate_loop + """
        intercept->PreCallValidateDestroyDebugReportCallbackEXT(instance, callback, pAllocator);
    }
    """ + precallrecord_loop + """
        intercept->PreCallRecordDestroyDebugReportCallbackEXT(instance, callback, pAllocator);
    }
    DispatchDestroyDebugReportCallbackEXT(layer_data, instance, callback, pAllocator);
    layer_destroy_report_callback(layer_data->report_data, callback, pAllocator);
    """ + postcallrecord_loop + """
        intercept->PostCallRecordDestroyDebugReportCallbackEXT(instance, callback, pAllocator);
    }
}
"""

    inline_custom_source_postamble = """
// loader-layer interface v0, just wrappers since there is only a layer

VK_LAYER_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateInstanceExtensionProperties(const char *pLayerName, uint32_t *pCount,
                                                                                      VkExtensionProperties *pProperties) {
    return vulkan_uber_layer::EnumerateInstanceExtensionProperties(pLayerName, pCount, pProperties);
}

VK_LAYER_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateInstanceLayerProperties(uint32_t *pCount,
                                                                                  VkLayerProperties *pProperties) {
    return vulkan_uber_layer::EnumerateInstanceLayerProperties(pCount, pProperties);
}

VK_LAYER_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateDeviceLayerProperties(VkPhysicalDevice physicalDevice, uint32_t *pCount,
                                                                                VkLayerProperties *pProperties) {
    // the layer command handles VK_NULL_HANDLE just fine internally
    assert(physicalDevice == VK_NULL_HANDLE);
    return vulkan_uber_layer::EnumerateDeviceLayerProperties(VK_NULL_HANDLE, pCount, pProperties);
}

VK_LAYER_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateDeviceExtensionProperties(VkPhysicalDevice physicalDevice,
                                                                                    const char *pLayerName, uint32_t *pCount,
                                                                                    VkExtensionProperties *pProperties) {
    // the layer command handles VK_NULL_HANDLE just fine internally
    assert(physicalDevice == VK_NULL_HANDLE);
    return vulkan_uber_layer::EnumerateDeviceExtensionProperties(VK_NULL_HANDLE, pLayerName, pCount, pProperties);
}

VK_LAYER_EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice dev, const char *funcName) {
    return vulkan_uber_layer::GetDeviceProcAddr(dev, funcName);
}

VK_LAYER_EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance instance, const char *funcName) {
    return vulkan_uber_layer::GetInstanceProcAddr(instance, funcName);
}

VK_LAYER_EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vk_layerGetPhysicalDeviceProcAddr(VkInstance instance,
                                                                                           const char *funcName) {
    return vulkan_uber_layer::GetPhysicalDeviceProcAddr(instance, funcName);
}

VK_LAYER_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface *pVersionStruct) {
    assert(pVersionStruct != NULL);
    assert(pVersionStruct->sType == LAYER_NEGOTIATE_INTERFACE_STRUCT);

    // Fill in the function pointers if our version is at least capable of having the structure contain them.
    if (pVersionStruct->loaderLayerInterfaceVersion >= 2) {
        pVersionStruct->pfnGetInstanceProcAddr = vkGetInstanceProcAddr;
        pVersionStruct->pfnGetDeviceProcAddr = vkGetDeviceProcAddr;
        pVersionStruct->pfnGetPhysicalDeviceProcAddr = vk_layerGetPhysicalDeviceProcAddr;
    }

    return VK_SUCCESS;
}"""


    def __init__(self,
                 errFile = sys.stderr,
                 warnFile = sys.stderr,
                 diagFile = sys.stdout):
        OutputGenerator.__init__(self, errFile, warnFile, diagFile)
        # Internal state - accumulators for different inner block text
        self.sections = dict([(section, []) for section in self.ALL_SECTIONS])
        self.intercepts = []
        self.layer_factory = ''                     # String containing base layer factory class definition

    # Check if the parameter passed in is a pointer to an array
    def paramIsArray(self, param):
        return param.attrib.get('len') is not None

    # Check if the parameter passed in is a pointer
    def paramIsPointer(self, param):
        ispointer = False
        for elem in param:
            if ((elem.tag is not 'type') and (elem.tail is not None)) and '*' in elem.tail:
                ispointer = True
        return ispointer

    # Check if an object is a non-dispatchable handle
    def isHandleTypeNonDispatchable(self, handletype):
        handle = self.registry.tree.find("types/type/[name='" + handletype + "'][@category='handle']")
        if handle is not None and handle.find('type').text == 'VK_DEFINE_NON_DISPATCHABLE_HANDLE':
            return True
        else:
            return False

    # Check if an object is a dispatchable handle
    def isHandleTypeDispatchable(self, handletype):
        handle = self.registry.tree.find("types/type/[name='" + handletype + "'][@category='handle']")
        if handle is not None and handle.find('type').text == 'VK_DEFINE_HANDLE':
            return True
        else:
            return False
    #
    #
    def beginFile(self, genOpts):
        OutputGenerator.beginFile(self, genOpts)
        # Output Copyright
        write(self.inline_copyright_message, file=self.outFile)
        # Multiple inclusion protection
        self.header = False
        if (self.genOpts.filename and 'h' == self.genOpts.filename[-1]):
            self.header = True
            write('#pragma once', file=self.outFile)
            self.newline()
        if self.header:
            write(self.inline_custom_header_preamble, file=self.outFile)
        else:
            write(self.inline_custom_source_preamble, file=self.outFile)
        self.layer_factory += self.inline_custom_header_class_definition
    #
    #
    def endFile(self):
        # Finish C++ namespace and multiple inclusion protection
        self.newline()
        if not self.header:
            # Record intercepted procedures
            write('// Map of all APIs to be intercepted by this layer', file=self.outFile)
            write('const std::unordered_map<std::string, void*> name_to_funcptr_map = {', file=self.outFile)
            write('\n'.join(self.intercepts), file=self.outFile)
            write('};\n', file=self.outFile)
            self.newline()
            write('} // namespace vulkan_uber_layer', file=self.outFile)
        if self.header:
            self.newline()
            # Output Layer Factory Class Definitions
            self.layer_factory += '};\n\n'
            self.layer_factory += 'extern std::unordered_map<void*, ValidationObject*> layer_data_map;'
            write(self.layer_factory, file=self.outFile)
        else:
            write(self.inline_custom_source_postamble, file=self.outFile)
        # Finish processing in superclass
        OutputGenerator.endFile(self)

    def beginFeature(self, interface, emit):
        # Start processing in superclass
        OutputGenerator.beginFeature(self, interface, emit)
        # Get feature extra protect
        self.featureExtraProtect = GetFeatureProtect(interface)
        # Accumulate includes, defines, types, enums, function pointer typedefs, end function prototypes separately for this
        # feature. They're only printed in endFeature().
        self.sections = dict([(section, []) for section in self.ALL_SECTIONS])

    def endFeature(self):
        # Actually write the interface to the output file.
        if (self.emit):
            self.newline()
            # If type declarations are needed by other features based on this one, it may be necessary to suppress the ExtraProtect,
            # or move it below the 'for section...' loop.
            if (self.featureExtraProtect != None):
                write('#ifdef', self.featureExtraProtect, file=self.outFile)
            for section in self.TYPE_SECTIONS:
                contents = self.sections[section]
                if contents:
                    write('\n'.join(contents), file=self.outFile)
                    self.newline()
            if (self.sections['command']):
                write('\n'.join(self.sections['command']), end=u'', file=self.outFile)
                self.newline()
            if (self.featureExtraProtect != None):
                write('#endif //', self.featureExtraProtect, file=self.outFile)
        # Finish processing in superclass
        OutputGenerator.endFeature(self)
    #
    # Append a definition to the specified section
    def appendSection(self, section, text):
        self.sections[section].append(text)
    #
    # Type generation
    def genType(self, typeinfo, name, alias):
        pass
    #
    # Struct (e.g. C "struct" type) generation. This is a special case of the <type> tag where the contents are
    # interpreted as a set of <member> tags instead of freeform C type declarations. The <member> tags are just like <param>
    # tags - they are a declaration of a struct or union member. Only simple member declarations are supported (no nested
    # structs etc.)
    def genStruct(self, typeinfo, typeName):
        OutputGenerator.genStruct(self, typeinfo, typeName)
        body = 'typedef ' + typeinfo.elem.get('category') + ' ' + typeName + ' {\n'
        # paramdecl = self.makeCParamDecl(typeinfo.elem, self.genOpts.alignFuncParam)
        for member in typeinfo.elem.findall('.//member'):
            body += self.makeCParamDecl(member, self.genOpts.alignFuncParam)
            body += ';\n'
        body += '} ' + typeName + ';\n'
        self.appendSection('struct', body)
    #
    # Group (e.g. C "enum" type) generation. These are concatenated together with other types.
    def genGroup(self, groupinfo, groupName, alias):
        pass
    # Enumerant generation
    # <enum> tags may specify their values in several ways, but are usually just integers.
    def genEnum(self, enuminfo, name, alias):
        pass
    #
    # Customize Cdecl for layer factory base class
    def BaseClassCdecl(self, elem, name):
        raw = self.makeCDecls(elem)[1]

        # Toss everything before the undecorated name
        prototype = raw.split("VKAPI_PTR *PFN_vk")[1]
        prototype = prototype.replace(")", "", 1)
        prototype = prototype.replace(";", " {};")

        # Build up pre/post call virtual function declarations
        pre_call_validate = 'virtual bool PreCallValidate' + prototype
        pre_call_validate = pre_call_validate.replace("{}", " { return false; }")
        pre_call_record = 'virtual void PreCallRecord' + prototype
        post_call_record = 'virtual void PostCallRecord' + prototype
        return '        %s\n        %s\n        %s\n' % (pre_call_validate, pre_call_record, post_call_record)
    #
    # Command generation
    def genCmd(self, cmdinfo, name, alias):
        ignore_functions = [
        'vkEnumerateInstanceVersion'
        ]

        if name in ignore_functions:
            return

        if self.header: # In the header declare all intercepts
            self.appendSection('command', '')
            self.appendSection('command', self.makeCDecls(cmdinfo.elem)[0])
            if (self.featureExtraProtect != None):
                self.intercepts += [ '#ifdef %s' % self.featureExtraProtect ]
                self.layer_factory += '#ifdef %s\n' % self.featureExtraProtect
            # Update base class with virtual function declarations
            self.layer_factory += self.BaseClassCdecl(cmdinfo.elem, name)
            # Update function intercepts
            self.intercepts += [ '    {"%s", (void*)%s},' % (name,name[2:]) ]
            if (self.featureExtraProtect != None):
                self.intercepts += [ '#endif' ]
                self.layer_factory += '#endif\n'
            return

        manual_functions = [
            # Include functions here to be interecpted w/ manually implemented function bodies
            'vkGetDeviceProcAddr',
            'vkGetInstanceProcAddr',
            'vkGetPhysicalDeviceProcAddr',
            'vkCreateDevice',
            'vkDestroyDevice',
            'vkCreateInstance',
            'vkDestroyInstance',
            'vkCreateDebugReportCallbackEXT',
            'vkDestroyDebugReportCallbackEXT',
            'vkEnumerateInstanceLayerProperties',
            'vkEnumerateInstanceExtensionProperties',
            'vkEnumerateDeviceLayerProperties',
            'vkEnumerateDeviceExtensionProperties',
        ]
        if name in manual_functions:
            self.intercepts += [ '    {"%s", (void*)%s},' % (name,name[2:]) ]
            return
        # Record that the function will be intercepted
        if (self.featureExtraProtect != None):
            self.intercepts += [ '#ifdef %s' % self.featureExtraProtect ]
        self.intercepts += [ '    {"%s", (void*)%s},' % (name,name[2:]) ]
        if (self.featureExtraProtect != None):
            self.intercepts += [ '#endif' ]
        OutputGenerator.genCmd(self, cmdinfo, name, alias)
        #
        decls = self.makeCDecls(cmdinfo.elem)
        self.appendSection('command', '')
        self.appendSection('command', '%s {' % decls[0][:-1])
        # Setup common to call wrappers. First parameter is always dispatchable
        dispatchable_type = cmdinfo.elem.find('param/type').text
        dispatchable_name = cmdinfo.elem.find('param/name').text
        # Default to device
        device_or_instance = 'device'
        dispatch_table_name = 'VkLayerDispatchTable'
        # Set to instance as necessary
        if dispatchable_type in ["VkPhysicalDevice", "VkInstance"] or name == 'vkCreateInstance':
            device_or_instance = 'instance'
            dispatch_table_name = 'VkLayerInstanceDispatchTable'
        self.appendSection('command', '    auto layer_data = GetLayerDataPtr(get_dispatch_key(%s), layer_data_map);' % (dispatchable_name))
        api_function_name = cmdinfo.elem.attrib.get('name')
        params = cmdinfo.elem.findall('param/name')
        paramstext = ', '.join([str(param.text) for param in params])
        API = api_function_name.replace('vk','Dispatch') + '(layer_data, '

        # Declare result variable, if any.
        return_map = {
            'void': 'return;',
            'VkResult': 'return VK_ERROR_VALIDATION_FAILED_EXT;',
            'PFN_vkVoidFunction': 'return nullptr;',
            'VkBool32': 'return VK_FALSE;',
            }
        resulttype = cmdinfo.elem.find('proto/type')
        assignresult = ''
        if (resulttype.text != 'void'):
            assignresult = resulttype.text + ' result = '

        # Set up skip and locking
        self.appendSection('command', '    {')
        self.appendSection('command', '        bool skip = false;')
        self.appendSection('command', '        std::lock_guard<std::mutex> lock(global_lock);')

        # Generate pre-call validation source code
        self.appendSection('command', '        %s' % self.precallvalidate_loop)
        self.appendSection('command', '            skip |= intercept->PreCallValidate%s(%s);' % (api_function_name[2:], paramstext))
        self.appendSection('command', '            if (skip) %s' % return_map[resulttype.text])
        self.appendSection('command', '        }')

        # Generate pre-call state recording source code
        self.appendSection('command', '        %s' % self.precallrecord_loop)
        self.appendSection('command', '            intercept->PreCallRecord%s(%s);' % (api_function_name[2:], paramstext))
        self.appendSection('command', '        }')
        self.appendSection('command', '    }')

        self.appendSection('command', '    ' + assignresult + API + paramstext + ');')

        # Generate post-call object processing source code
        return_check = ''
        if (resulttype.text == 'VkResult'):
            return_check = 'if (VK_SUCCESS == result) '
        self.appendSection('command', '    %s{' % return_check)
        self.appendSection('command', '        std::lock_guard<std::mutex> lock(global_lock);')
        self.appendSection('command', '        %s' % self.postcallrecord_loop)
        self.appendSection('command', '            intercept->PostCallRecord%s(%s);' % (api_function_name[2:], paramstext))
        self.appendSection('command', '        }')
        self.appendSection('command', '    }')

        # Return result variable, if any.
        if (resulttype.text != 'void'):
            self.appendSection('command', '    return result;')
        self.appendSection('command', '}')
    #
    # Override makeProtoName to drop the "vk" prefix
    def makeProtoName(self, name, tail):
        return self.genOpts.apientry + name[2:] + tail
