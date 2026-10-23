
// I stole this code from the NMEA2000 library. It is used to encode and decode messages.


#pragma once

#include <Arduino.h>

namespace msgEncode {

/** \brief  Constant "Not Available" for a double value*/
const double   N2kDoubleNA=-1e9;
/** \brief  Constant "Not Available" for a float value*/
const float    N2kFloatNA=-1e9;
/** \brief  Constant "Not Available" for a unsigned 8bit int value*/
const uint8_t  N2kUInt8NA=0xff;
/** \brief  Constant "Not Available" for a signed 8bit value*/
const int8_t   N2kInt8NA=0x7f;
/** \brief  Constant "Not Available" for a unsigned 16bit int value*/
const uint16_t N2kUInt16NA=0xffff;
/** \brief  Constant "Not Available" for a signed 16bit int value*/
const int16_t  N2kInt16NA=0x7fff;
/** \brief  Constant "Not Available" for a unsigned 32bit int value*/
const uint32_t N2kUInt32NA=0xffffffff;
/** \brief  Constant "Not Available" for a signed 32bit int value*/
const int32_t  N2kInt32NA=0x7fffffff;
/** \brief  Constant "Not Available" for a unsigned 64bit int value*/
const uint64_t N2kUInt64NA=0xffffffffffffffffLL;
/** \brief  Constant "Not Available" for a signed 64bit int  value*/
const int64_t  N2kInt64NA=0x7fffffffffffffffLL;

#ifndef BIT
#define BIT(n) (1 << n)
#endif

//*****************************************************************************
template<typename T>
T GetBuf(size_t len, int& index, const unsigned char* buf) {
  T v{0};

  // This could be improved by casting the buffer to a pointer of T and
  // doing a direct copy. That is, if unaligned data access is allowed.
  memcpy(&v, &buf[index], len);
  index += len;

#if defined(HOST_IS_BIG_ENDIAN)
  v = byteswap(v);
#endif

  return v;
}

/**************************************************************************//**
 * \brief Verify that the specified value is equal to "Not available".
 * 
 * \param v  {double} Value to be verified
 * \return true:      Value equal to "Not available"
 * \return false:     Value not equal to "Not available"
 */
inline bool N2kIsNA(double v) { return v==N2kDoubleNA; }



//*****************************************************************************
template<typename T>
void SetBuf(T v, size_t len, int& index, unsigned char* buf) {
#if defined(HOST_IS_BIG_ENDIAN)
  v = byteswap(v);
#endif

  // This could be improved by casting the buffer to a pointer of T and
  // doing a direct copy. That is, if unaligned data access is allowed.
  memcpy(&buf[index], &v, len);
  index += len;
}

//*****************************************************************************
void SetBufDouble(double v, int &index, unsigned char *buf) {
  if ( sizeof(double)==8 && !N2kIsNA(v) ) {
    int64_t iv;
    memcpy(&iv,&v,8);
    SetBuf(iv, 8, index, buf);
  } else {  // on AVR double=float
    SetBuf((int64_t)N2kInt64NA,8,index,buf);
  }
}

//*****************************************************************************
void SetBufFloat(float v, int &index, unsigned char *buf) {
  int32_t iv;
  if ( !N2kIsNA(v) ) {
    memcpy(&iv,&v,4);
  } else {
    iv=N2kInt32NA;
  }
  SetBuf(iv, 4, index, buf);
}

#define N2kInt8OR 0x7e
#define N2kUInt8OR 0xfe
#define N2kInt16OR 0x7ffe
#define N2kUInt16OR 0xfffe
#define N2kInt32OR 0x7ffffffe
#define N2kUInt32OR 0xfffffffe

#define N2kInt32Min -2147483648L
#define N2kInt24OR  8388606L
#define N2kInt24Min -8388608L
#define N2kInt16Min -32768
#define N2kInt8Min  -128

//*****************************************************************************
void SetBuf8ByteDouble(double v, double precision, int &index, unsigned char *buf) {
  int64_t vll;
  if ( !N2kIsNA(v) ) {
    if ( sizeof(double)<8 ) {
      double fp=precision*1e6;
      int64_t fpll=1/fp;
      vll=v*1e6L;
      vll*=fpll;
    } else {
      vll=v/precision;
    }
  } else {
    vll=N2kInt64NA;
  }
  SetBuf(vll, 8, index, buf);
}

//*****************************************************************************
void SetBuf4ByteDouble(double v, double precision, int &index, unsigned char *buf) {
  double vd=round((v/precision));
  int32_t vi = (vd>=N2kInt32Min && vd<N2kInt32OR)?(int32_t)vd:N2kInt32OR;
  SetBuf<int32_t>(vi, 4, index, buf);
}

//*****************************************************************************
void SetBuf4ByteUDouble(double v, double precision, int &index, unsigned char *buf) {
  double vd=round((v/precision));
  uint32_t vi = (vd>=0 && vd<N2kUInt32OR)?(uint32_t)vd:N2kUInt32OR;
  SetBuf<uint32_t>(vi, 4, index, buf);
}

//*****************************************************************************
void SetBuf3ByteDouble(double v, double precision, int &index, unsigned char *buf) {
  double vd=round((v/precision));
  int32_t vi = (vd>=N2kInt24Min && vd<N2kInt24OR)?(int32_t)vd:N2kInt24OR;
  SetBuf<int32_t>(vi, 3, index, buf);
}

//*****************************************************************************
int16_t GetBuf2ByteInt(int &index, const unsigned char *buf) {
  return GetBuf<int16_t>(2, index, buf);
}

//*****************************************************************************
uint16_t GetBuf2ByteUInt(int &index, const unsigned char *buf) {
  return GetBuf<uint16_t>(2, index, buf);
}

//*****************************************************************************
uint32_t GetBuf3ByteUInt(int &index, const unsigned char *buf) {
  return GetBuf<uint32_t>(3, index, buf);
}

//*****************************************************************************
uint32_t GetBuf4ByteUInt(int &index, const unsigned char *buf) {
  return GetBuf<uint32_t>(4, index, buf);
}

//*****************************************************************************
uint64_t GetBuf8ByteUInt(int &index, const unsigned char *buf) {
  return GetBuf<uint64_t>(8, index, buf);
}

//*****************************************************************************
double GetBuf1ByteDouble(double precision, int &index, const unsigned char *buf, double def) {
  int8_t vl = GetBuf<int8_t>(1, index, buf);
  if (vl==0x7f) return def;

  return vl * precision;
}

//*****************************************************************************
double GetBuf1ByteUDouble(double precision, int &index, const unsigned char *buf, double def) {
  uint8_t vl = GetBuf<uint8_t>(1, index, buf);
  if (vl==0xff) return def;

  return vl * precision;
}

//*****************************************************************************
double GetBuf2ByteDouble(double precision, int &index, const unsigned char *buf, double def) {
  int16_t vl = GetBuf<int16_t>(2, index, buf);
  if (vl==0x7fff) return def;

  return vl * precision;
}

//*****************************************************************************
double GetBuf2ByteUDouble(double precision, int &index, const unsigned char *buf, double def) {
  uint16_t vl = GetBuf<uint16_t>(2, index, buf);
  if (vl==0xffff) return def;

  return vl * precision;
}

//*****************************************************************************
double GetBuf8ByteDouble(double precision, int &index, const unsigned char *buf, double def) {
  int64_t vl = GetBuf<int64_t>(8, index, buf);
  if (vl==0x7fffffffffffffffLL) return def;

  return vl * precision;
}

//*****************************************************************************
double GetBufDouble(int &index, const unsigned char *buf, double def) {
  int64_t vl = GetBuf<int64_t>(8, index, buf);
  double ret;
   // On avr double==float, so we test it also. Currently no handling for avr.
  if ( sizeof(double)==8 && !N2kIsNA(vl) ) {
    memcpy(&ret,&vl,8);
    if ( isnan(ret) ) ret=def;
  } else {
    ret=def;
  }
  return ret;
}

//*****************************************************************************
float GetBufFloat(int &index, const unsigned char *buf, float def) {
  int32_t vl = GetBuf<int32_t>(4, index, buf);
  float ret;
  if ( !N2kIsNA(vl) ) {
    memcpy(&ret,&vl,4);
    if ( isnan(ret) ) ret=def;
  } else { // On avr double==float
    ret=def;
  }
  return ret;
}

//*****************************************************************************
double GetBuf3ByteDouble(double precision, int &index, const unsigned char *buf, double def) {
  int32_t vl = GetBuf<int32_t>(3, index, buf);
  if (vl==0x007fffff) return def;

  return vl * precision;
}

//*****************************************************************************
double GetBuf4ByteDouble(double precision, int &index, const unsigned char *buf, double def) {
  int32_t vl = GetBuf<int32_t>(4, index, buf);
  if (vl==0x7fffffff) return def;

  return vl * precision;
}

//*****************************************************************************
double GetBuf4ByteUDouble(double precision, int &index, const unsigned char *buf, double def) {
  uint32_t vl = GetBuf<uint32_t>(4, index, buf);
  if (vl==0xffffffff) return def;

  return vl * precision;
}

//*****************************************************************************
void SetBuf2ByteDouble(double v, double precision, int &index, unsigned char *buf) {
  double vd=round((v/precision));
  int16_t vi = (vd>=N2kInt16Min && vd<N2kInt16OR)?(int16_t)vd:N2kInt16OR;
  SetBuf(vi, 2, index, buf);
}

//*****************************************************************************
void SetBuf2ByteUDouble(double v, double precision, int &index, unsigned char *buf) {
  double vd=round((v/precision));
  uint16_t vi = (vd>=0 && vd<N2kUInt16OR)?(uint16_t)vd:N2kUInt16OR;
  SetBuf(vi, 2, index, buf);
}

//*****************************************************************************
void SetBuf1ByteDouble(double v, double precision, int &index, unsigned char *buf) {
  double vd=round((v/precision));
  int8_t vi = (vd>=N2kInt8Min && vd<N2kInt8OR)?(int8_t)vd:N2kInt8OR;
  SetBuf(vi, 1, index, buf);
}

//*****************************************************************************
void SetBuf1ByteUDouble(double v, double precision, int &index, unsigned char *buf) {
  double vd=round((v/precision));
  uint8_t vi = (vd>=0 && vd<N2kUInt8OR)?(uint8_t)vd:N2kUInt8OR;
  SetBuf(vi, 1, index, buf);
}

//*****************************************************************************
void SetBuf2ByteInt(int16_t v, int &index, unsigned char *buf) {
  SetBuf(v, 2, index, buf);
}

//*****************************************************************************
void SetBuf2ByteUInt(uint16_t v, int &index, unsigned char *buf) {
  SetBuf(v, 2, index, buf);
}

//*****************************************************************************
void SetBuf3ByteInt(int32_t v, int &index, unsigned char *buf) {
  SetBuf(v, 3, index, buf);
}

//*****************************************************************************
void SetBuf4ByteUInt(uint32_t v, int &index, unsigned char *buf) {
  SetBuf(v, 4, index, buf);
}

//*****************************************************************************
void SetBufUInt64(uint64_t v, int &index, unsigned char *buf) {
  SetBuf(v, 8, index, buf);
}

//*****************************************************************************
void SetBufStr(const char *str, int len, int &index, unsigned char *buf, bool UsePgm, unsigned char fillChar) {
  int i=0;
  if ( UsePgm ) {
    for (; i<len && str[i]!=0; i++, index++) {
      buf[index]=pgm_read_byte(&(str[i]));
    }
  } else {
    for (; i<len && str[i]!=0; i++, index++) {
      buf[index]=str[i];
    }
  }
  for (; i<len; i++, index++) {
    buf[index]=fillChar;
  }
}
}  // namespace msgEncode