
package main

import (
    "net/http"
    "net/http/httptest"
    "testing"

    "github.com/gin-gonic/gin"
)

func TestApplicationRoute(t *testing.T) {
    gin.SetMode(gin.TestMode)

    router := gin.New()

    router.GET("/test", func(c *gin.Context) {
        c.String(http.StatusOK, "SkillPulse is working")
    })

    request := httptest.NewRequest("GET", "/test", nil)
    response := httptest.NewRecorder()

    router.ServeHTTP(response, request)

    if response.Code != http.StatusOK {
        t.Errorf("Expected status 200, got %d", response.Code)
    }
}

