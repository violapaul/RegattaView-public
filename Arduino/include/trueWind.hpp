#pragma once

/******************************************************************************
 *                      Code generated with SymPy 1.11.1                      *
 *                                                                            *
 *              See http://www.sympy.org/ for more information.               *
 *                                                                            *
 *                      This file is part of 'WindCalc'                       *
 ******************************************************************************/
#include <math.h>
#include <ArduinoEigen.h>

// Let's use Eignen

/*****************************************************************************
 * File contains code to process, smooth, and estimate boat state.
 * 
 
 * This file contains code to estimate TWD and TWS using an inverse technique.  The code predicts the observed 
 * quantities given the current estimate for TWD and TWS.  The code then computes the gradient of the cost function 
 * and using gauss newton updates the estimate for TWD and TWS.  

    Note, AWA, ASS, SOG, and COG are observed quantities.
*/

namespace trueWind {

#define FLT float

// computes the aws (apparent wind speed).
FLT aws_function(FLT COG, FLT SOG, FLT TWD, FLT TWS) {

    FLT aws_function_result;
    aws_function_result = sqrt(pow(-SOG * sin(COG) - TWS * sin(TWD), 2) + pow(-SOG * cos(COG) - TWS * cos(TWD), 2));
    return aws_function_result;
}

// computes the awa (apparent wind angle).
FLT awa_function(FLT COG, FLT HDG, FLT SOG, FLT TWD, FLT TWS) {

    FLT awa_function_result;
    awa_function_result = -HDG + atan2(SOG * sin(COG) + TWS * sin(TWD), SOG * cos(COG) + TWS * cos(TWD));
    return awa_function_result;
}

// computes partial derivative of aws given twd
FLT d_aws_d_twd_function(FLT COG, FLT SOG, FLT TWD, FLT TWS) {

    FLT d_aws_d_twd_function_result;
    d_aws_d_twd_function_result =
        SOG * TWS * sin(COG - TWD) / sqrt(pow(SOG, 2) + 2 * SOG * TWS * cos(COG - TWD) + pow(TWS, 2));
    return d_aws_d_twd_function_result;
}

// computes the partial of awa given twd
FLT d_awa_d_twd_function(FLT COG, FLT SOG, FLT TWD, FLT TWS) {

    FLT d_awa_d_twd_function_result;
    d_awa_d_twd_function_result =
        TWS * (SOG * cos(COG - TWD) + TWS) / (pow(SOG, 2) + 2 * SOG * TWS * cos(COG - TWD) + pow(TWS, 2));
    return d_awa_d_twd_function_result;
}

// computes the partial of aws given tws
FLT d_aws_d_tws_function(FLT COG, FLT SOG, FLT TWD, FLT TWS) {

    FLT d_aws_d_tws_function_result;
    d_aws_d_tws_function_result =
        (SOG * cos(COG - TWD) + TWS) / sqrt(pow(SOG, 2) + 2 * SOG * TWS * cos(COG - TWD) + pow(TWS, 2));
    return d_aws_d_tws_function_result;
}

// computes the partial of awa given tws
FLT d_awa_d_tws_function(FLT COG, FLT SOG, FLT TWD, FLT TWS) {

    FLT d_awa_d_tws_function_result;
    d_awa_d_tws_function_result = -SOG * sin(COG - TWD) / (pow(SOG, 2) + 2 * SOG * TWS * cos(COG - TWD) + pow(TWS, 2));
    return d_awa_d_tws_function_result;
}

#define TWD_BASE_GAIN 0.1
#define AWA_MAX_RESIDUAL 10.0
float twdGain = TWD_BASE_GAIN;

#define TWS_BASE_GAIN 0.03
#define AWS_MAX_RESIDUAL 4.0
float twsGain = TWS_BASE_GAIN;

void compute_update(FLT AWA, FLT AWS, FLT COG, FLT SOG, FLT HDG, FLT& TWD, FLT& TWS) {
}

void compute_updateReal(FLT AWA, FLT AWS, FLT COG, FLT SOG, FLT HDG, FLT& TWD, FLT& TWS) {
    FLT awa_rad = AWA * M_PI / 180.0;
    FLT twd_rad = TWD * M_PI / 180.0;
    FLT cog_rad = COG * M_PI / 180.0;
    FLT hdg_rad = HDG * M_PI / 180.0;

    FLT predicted_AWA = awa_function(cog_rad, hdg_rad, SOG, twd_rad, TWS);
    FLT predicted_AWS = aws_function(cog_rad, SOG, twd_rad, TWS);
    FLT d_awa_d_twd = d_awa_d_twd_function(cog_rad, SOG, twd_rad, TWS);
    FLT d_awa_d_tws = d_awa_d_tws_function(cog_rad, SOG, twd_rad, TWS);
    FLT d_aws_d_twd = d_aws_d_twd_function(cog_rad, SOG, twd_rad, TWS);
    FLT d_aws_d_tws = d_aws_d_tws_function(cog_rad, SOG, twd_rad, TWS);

    FLT residual_awa = awa_rad - predicted_AWA;
    FLT residual_aws = AWS - predicted_AWS;
    // Careful with angle wrap around
    if (residual_awa > M_PI) {
        residual_awa -= 2 * M_PI;
    } else if (residual_awa < -M_PI) {
        residual_awa += 2 * M_PI;
    }
    // J matrix in eigen
    Eigen::Matrix<FLT, 2, 2> J;
    J(0, 0) = d_awa_d_twd;
    J(0, 1) = d_awa_d_tws;
    J(1, 0) = d_aws_d_twd;
    J(1, 1) = d_aws_d_tws;

    // JtJ matrix in eigen plus epsilon the identiry
    Eigen::Matrix<FLT, 2, 2> JtJ = J.transpose() * J + 0.0001 * Eigen::Matrix<FLT, 2, 2>::Identity();
    // JtJinv matrix in eigen
    Eigen::Matrix<FLT, 2, 2> JtJinv = JtJ.inverse();
    // JtJinvJt matrix in eigen
    Eigen::Matrix<FLT, 2, 2> JtJinvJt = JtJinv * J.transpose();
    // delta matrix in eigen
    Eigen::Matrix<FLT, 2, 1> delta = JtJinvJt * Eigen::Matrix<FLT, 2, 1>(residual_awa, residual_aws);

    if (residual_awa > AWA_MAX_RESIDUAL) {
        twdGain += TWD_BASE_GAIN;
    } else {
        twdGain = (twdGain + TWD_BASE_GAIN) / 2.0;
    }

    if (residual_aws > AWS_MAX_RESIDUAL) {
        twsGain += TWS_BASE_GAIN;
    } else {
        twsGain = (twsGain + TWS_BASE_GAIN) / 2.0;
    }

    // Convert delta back to degrees
    delta(0, 0) *= 180.0 / M_PI;

    // update TWD but be careful about angle wrap around
    TWD = TWD + delta(0, 0) * twdGain;
    if (TWD > 360) {
        TWD -= 360;
    } else if (TWD < 0) {
        TWD += 360;
    }
    TWS = TWS + delta(1, 0) * twsGain;
}

}  // namespace trueWind