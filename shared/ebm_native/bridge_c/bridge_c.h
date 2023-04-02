// Copyright (c) 2023 The InterpretML Contributors
// Licensed under the MIT license.
// Author: Paul Koch <code@koch.ninja>

#ifndef BRIDGE_C_H
#define BRIDGE_C_H

#include <stdlib.h> // free

#include "ebm_native.h" // ErrorEbm, BoolEbm, etc..

#ifdef __cplusplus
extern "C" {
#define INTERNAL_IMPORT_EXPORT_BODY extern "C"
#else // __cplusplus
#define INTERNAL_IMPORT_EXPORT_BODY extern
#endif // __cplusplus

#define INTERNAL_IMPORT_EXPORT_INCLUDE extern

// TODO: someday flip FloatFast to float32
typedef double FloatFast;

typedef size_t StorageDataType;
typedef UIntEbm ActiveDataType; // TODO: in most places we could use size_t for this and only use the uint64 version where we have cross-platform considerations.

struct ApplyUpdateBridge {
   // TODO: remove these first 2 items
   ptrdiff_t m_cRuntimeScores;
   BoolEbm m_bHessianNeeded;
   // End REMOVE section



   ptrdiff_t m_cClasses;
   ptrdiff_t m_cPack;
   bool m_bCalcMetric;
   FloatFast * m_aMulticlassMidwayTemp;
   const FloatFast * m_aUpdateTensorScores;
   size_t m_cSamples;
   const StorageDataType * m_aPacked;
   const void * m_aTargets;
   const FloatFast * m_aWeights;
   FloatFast * m_aSampleScores;
   FloatFast * m_aGradientsAndHessians;
   double m_metricOut;
};

struct LossWrapper;

// these are extern "C" function pointers so we can't call anything other than an extern "C" function with them
typedef ErrorEbm (* APPLY_UPDATE_C)(const LossWrapper * const pLossWrapper, ApplyUpdateBridge * const pData);

struct LossWrapper {
   APPLY_UPDATE_C m_pApplyUpdateC;
   // everything below here the C++ *Loss specific class needs to fill out

   // this needs to be void since our Registrable object is C++ visible and we cannot define it initially 
   // here in this C file since our object needs to be a POD and thus can't inherit data
   // and it cannot be empty either since empty structures are not compliant in all C compilers
   // https://stackoverflow.com/questions/755305/empty-structure-in-c?rq=1
   void * m_pLoss;
   double m_updateMultiple;
   BoolEbm m_bLossHasHessian;
   BoolEbm m_bSuperSuperSpecialLossWhereTargetNotNeededOnlyMseLossQualifies;
   // these are C++ function pointer definitions that exist per-zone, and must remain hidden in the C interface
   void * m_pFunctionPointersCpp;
};

inline static void InitializeLossWrapperUnfailing(LossWrapper * const pLossWrapper) {
   pLossWrapper->m_pLoss = NULL;
   pLossWrapper->m_pFunctionPointersCpp = NULL;
}

inline static void FreeLossWrapperInternals(LossWrapper * const pLossWrapper) {
   free(pLossWrapper->m_pLoss);
   free(pLossWrapper->m_pFunctionPointersCpp);
}

struct Config {
   // don't use m_ notation here, mostly to make it cleaner for people writing *Loss classes
   size_t cOutputs;
};

INTERNAL_IMPORT_EXPORT_INCLUDE ErrorEbm CreateLoss_Cpu_64(
   const Config * const pConfig,
   const char * const sLoss,
   const char * const sLossEnd,
   LossWrapper * const pLossWrapperOut
);

INTERNAL_IMPORT_EXPORT_INCLUDE ErrorEbm CreateLoss_Cuda_32(
   const Config * const pConfig,
   const char * const sLoss,
   const char * const sLossEnd,
   LossWrapper * const pLossWrapperOut
);

INTERNAL_IMPORT_EXPORT_INCLUDE ErrorEbm CreateMetric_Cpu_64(
   const Config * const pConfig,
   const char * const sMetric,
   const char * const sMetricEnd
   //   MetricWrapper * const pMetricWrapperOut,
);

#ifdef __cplusplus
} // extern "C"
#endif // __cplusplus

#endif // BRIDGE_C_H
